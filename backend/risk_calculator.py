from argparse import ArgumentParser
from dataclasses import dataclass
from math import sqrt
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

from database import fetch_all, validate_database


TRADING_DAYS_PER_YEAR = 252
MINIMUM_OBSERVATIONS = 30
VAR_CONFIDENCE_LEVEL = 0.95
VALUE_ZERO_TOLERANCE = 1e-10

RISK_FREE_TICKER = "^TNX"
RISK_FREE_LOOKBACK_PERIOD = "1mo"


@dataclass
class RiskAnalysisResult:
    summary: dict[str, Any]
    security_metrics: pd.DataFrame
    correlation_matrix: pd.DataFrame
    covariance_matrix: pd.DataFrame
    risk_contribution: pd.DataFrame


def query_to_dataframe(query: str) -> pd.DataFrame:
    """Execute a database query and return the result as a DataFrame."""
    rows = fetch_all(query)

    if not rows:
        return pd.DataFrame()

    if hasattr(rows[0], "keys"):
        return pd.DataFrame([dict(row) for row in rows])

    return pd.DataFrame(rows)


def get_us_10_year_treasury_rate() -> dict[str, Any]:
    """
    Download the latest available US 10-year Treasury yield.

    Yahoo Finance reports ^TNX in percentage points.
    For example, 4.25 represents 4.25%, which is 0.0425
    when used in financial calculations.
    """
    data = yf.download(
        tickers=RISK_FREE_TICKER,
        period=RISK_FREE_LOOKBACK_PERIOD,
        interval="1d",
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        rounding=False,
        timeout=20,
        multi_level_index=False,
    )

    if data is None or data.empty:
        raise RuntimeError(
            f"No risk-free-rate data was returned for "
            f"{RISK_FREE_TICKER}."
        )

    if "Close" not in data.columns:
        raise RuntimeError(
            f"The Close column was not returned for "
            f"{RISK_FREE_TICKER}."
        )

    close_prices = data["Close"]

    # Handle a possible single-column DataFrame.
    if isinstance(close_prices, pd.DataFrame):
        close_prices = close_prices.iloc[:, 0]

    close_prices = pd.to_numeric(
        close_prices,
        errors="coerce",
    ).dropna()

    if close_prices.empty:
        raise RuntimeError(
            f"No valid Treasury yield was returned for "
            f"{RISK_FREE_TICKER}."
        )

    latest_yield_percent = float(close_prices.iloc[-1])
    observation_date = pd.Timestamp(close_prices.index[-1])

    if observation_date.tzinfo is not None:
        observation_date = observation_date.tz_localize(None)

    if latest_yield_percent <= 0:
        raise RuntimeError(
            f"Invalid US 10-year Treasury yield: "
            f"{latest_yield_percent}"
        )

    if latest_yield_percent >= 30:
        raise RuntimeError(
            f"The downloaded Treasury yield appears unreasonable: "
            f"{latest_yield_percent}%"
        )

    return {
        "rate": latest_yield_percent / 100.0,
        "percentage": latest_yield_percent,
        "source": f"Yahoo Finance {RISK_FREE_TICKER}",
        "observation_date": observation_date.strftime("%Y-%m-%d"),
    }


def resolve_risk_free_rate(
    risk_free_rate: float | None,
) -> dict[str, Any]:
    """Use ^TNX by default or accept a manual annual rate override."""
    if risk_free_rate is None:
        return get_us_10_year_treasury_rate()

    risk_free_rate = float(risk_free_rate)

    if risk_free_rate < 0:
        raise ValueError("Risk-free rate cannot be negative.")

    return {
        "rate": risk_free_rate,
        "percentage": risk_free_rate * 100.0,
        "source": "User override",
        "observation_date": None,
    }


def load_portfolio_daily(
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[pd.DataFrame, float]:
    """
    Load daily portfolio values and returns.

    The second returned value is the cumulative TWR immediately
    before the selected analysis period.
    """

    portfolio_daily = query_to_dataframe(
        """
        SELECT
            date,
            opening_value_usd,
            securities_value_usd,
            cash_balance_usd,
            closing_value_usd,
            external_inflow_usd,
            external_outflow_usd,
            daily_return,
            cumulative_twr
        FROM portfolio_daily
        ORDER BY date
        """
    )

    if portfolio_daily.empty:
        raise ValueError(
            "portfolio_daily is empty. Run "
            "portfolio_daily_calculator.py first."
        )

    portfolio_daily["date"] = pd.to_datetime(
        portfolio_daily["date"],
        errors="coerce",
    )

    portfolio_daily = portfolio_daily.dropna(
        subset=["date"]
    ).copy()

    numeric_columns = [
        "opening_value_usd",
        "securities_value_usd",
        "cash_balance_usd",
        "closing_value_usd",
        "external_inflow_usd",
        "external_outflow_usd",
        "daily_return",
        "cumulative_twr",
    ]

    for column in numeric_columns:
        portfolio_daily[column] = pd.to_numeric(
            portfolio_daily[column],
            errors="coerce",
        )

    portfolio_daily = (
        portfolio_daily
        .sort_values("date", kind="stable")
        .reset_index(drop=True)
    )

    selected_daily = portfolio_daily.copy()

    if start_date is not None:
        selected_daily = selected_daily[
            selected_daily["date"]
            >= pd.Timestamp(start_date)
        ]

    if end_date is not None:
        selected_daily = selected_daily[
            selected_daily["date"]
            <= pd.Timestamp(end_date)
        ]

    selected_daily = (
        selected_daily
        .sort_values("date", kind="stable")
        .reset_index(drop=True)
    )

    if selected_daily.empty:
        raise ValueError(
            "No portfolio_daily records exist inside "
            "the selected date range."
        )

    first_selected_date = selected_daily[
        "date"
    ].iloc[0]

    previous_rows = portfolio_daily[
        portfolio_daily["date"]
        < first_selected_date
    ]

    if previous_rows.empty:
        previous_cumulative_twr = 0.0
    else:
        previous_cumulative_twr = float(
            previous_rows[
                "cumulative_twr"
            ].iloc[-1]
        )

    return (
        selected_daily,
        previous_cumulative_twr,
    )

def calculate_period_twr(
    portfolio_daily: pd.DataFrame,
    previous_cumulative_twr: float,
) -> float:
    """
    Calculate selected-period TWR from stored cumulative TWR values.
    """

    cumulative_values = portfolio_daily[
        "cumulative_twr"
    ].dropna()

    if cumulative_values.empty:
        return float("nan")

    ending_cumulative_twr = float(
        cumulative_values.iloc[-1]
    )

    starting_wealth_factor = (
        1.0 + previous_cumulative_twr
    )

    if (
        abs(starting_wealth_factor)
        <= VALUE_ZERO_TOLERANCE
    ):
        raise ValueError(
            "The starting cumulative TWR produces "
            "an invalid wealth factor."
        )

    return float(
        (
            1.0 + ending_cumulative_twr
        )
        / starting_wealth_factor
        - 1.0
    )

def load_market_prices(
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Load historical adjusted market prices."""
    market_prices = query_to_dataframe(
        """
        SELECT
            ticker,
            date,
            adjusted_close_usd
        FROM market_prices
        ORDER BY ticker, date
        """
    )

    if market_prices.empty:
        raise ValueError(
            "market_prices is empty. Run market_data.py first."
        )

    required_columns = {
        "ticker",
        "date",
        "adjusted_close_usd",
    }

    missing_columns = required_columns.difference(
        market_prices.columns
    )

    if missing_columns:
        raise ValueError(
            "market_prices is missing required columns: "
            f"{sorted(missing_columns)}"
        )

    market_prices["ticker"] = (
        market_prices["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    market_prices["date"] = pd.to_datetime(
        market_prices["date"],
        errors="coerce",
    )

    market_prices["adjusted_close_usd"] = pd.to_numeric(
        market_prices["adjusted_close_usd"],
        errors="coerce",
    )

    market_prices = market_prices.dropna(
        subset=[
            "ticker",
            "date",
            "adjusted_close_usd",
        ]
    ).copy()

    market_prices = market_prices[
        market_prices["adjusted_close_usd"] > 0
    ]

    if start_date is not None:
        market_prices = market_prices[
            market_prices["date"] >= pd.Timestamp(start_date)
        ]

    if end_date is not None:
        market_prices = market_prices[
            market_prices["date"] <= pd.Timestamp(end_date)
        ]

    return (
        market_prices
        .sort_values(
            ["ticker", "date"],
            kind="stable",
        )
        .drop_duplicates(
            subset=["ticker", "date"],
            keep="last",
        )
        .reset_index(drop=True)
    )


def load_open_holdings() -> pd.DataFrame:
    """Load current open security positions."""
    holdings = query_to_dataframe(
        """
        SELECT *
        FROM holdings_summary
        ORDER BY ticker
        """
    )

    if holdings.empty:
        raise ValueError(
            "holdings_summary is empty. Run "
            "portfolio_calculator.py first."
        )

    if "ticker" not in holdings.columns:
        raise ValueError(
            "holdings_summary does not contain a ticker column."
        )

    holdings["ticker"] = (
        holdings["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    # Exclude cash because it does not have market-price returns.
    holdings = holdings[
        holdings["ticker"] != "CASH"
    ].copy()

    if "status" in holdings.columns:
        holdings["status"] = (
            holdings["status"]
            .astype(str)
            .str.strip()
            .str.upper()
        )

        holdings = holdings[
            holdings["status"] == "OPEN"
        ].copy()

    if "shares" in holdings.columns:
        holdings["shares"] = pd.to_numeric(
            holdings["shares"],
            errors="coerce",
        )

        holdings = holdings[
            holdings["shares"] > VALUE_ZERO_TOLERANCE
        ].copy()

    value_column_candidates = [
        "current_value_usd",
        "market_value_usd",
        "current_value",
    ]

    value_column = next(
        (
            column
            for column in value_column_candidates
            if column in holdings.columns
        ),
        None,
    )

    if value_column is None:
        raise ValueError(
            "holdings_summary must contain one of these columns: "
            "current_value_usd, market_value_usd, current_value."
        )

    holdings["analysis_value_usd"] = pd.to_numeric(
        holdings[value_column],
        errors="coerce",
    )

    holdings = holdings.dropna(
        subset=["analysis_value_usd"]
    ).copy()

    holdings = holdings[
        holdings["analysis_value_usd"] > VALUE_ZERO_TOLERANCE
    ].copy()

    if holdings.empty:
        raise ValueError(
            "No open security positions were found."
        )

    total_security_value = holdings[
        "analysis_value_usd"
    ].sum()

    if total_security_value <= VALUE_ZERO_TOLERANCE:
        raise ValueError(
            "Total open security value must be greater than zero."
        )

    holdings["weight"] = (
        holdings["analysis_value_usd"]
        / total_security_value
    )

    return holdings.reset_index(drop=True)


def create_security_return_matrix(
    market_prices: pd.DataFrame,
    tickers: list[str],
) -> pd.DataFrame:
    """Create daily security returns from adjusted closing prices."""
    selected_prices = market_prices[
        market_prices["ticker"].isin(tickers)
    ].copy()

    price_matrix = selected_prices.pivot_table(
        index="date",
        columns="ticker",
        values="adjusted_close_usd",
        aggfunc="last",
    )

    price_matrix = price_matrix.sort_index()

    security_returns = price_matrix.pct_change(
        fill_method=None
    )

    security_returns = security_returns.replace(
        [np.inf, -np.inf],
        np.nan,
    )

    security_returns = security_returns.dropna(
        how="all"
    )

    return security_returns


def calculate_maximum_drawdown(
    returns: pd.Series,
) -> float:
    """Calculate maximum drawdown from a return series."""
    clean_returns = returns.dropna()

    if clean_returns.empty:
        return float("nan")

    wealth_index = (1.0 + clean_returns).cumprod()
    running_maximum = wealth_index.cummax()

    drawdown = (
        wealth_index / running_maximum
    ) - 1.0

    return float(drawdown.min())


def calculate_historical_var(
    returns: pd.Series,
    confidence_level: float = VAR_CONFIDENCE_LEVEL,
) -> float:
    """Calculate positive historical daily loss at the selected level."""
    clean_returns = returns.dropna()

    if clean_returns.empty:
        return float("nan")

    lower_quantile = clean_returns.quantile(
        1.0 - confidence_level
    )

    return float(max(0.0, -lower_quantile))


def calculate_historical_cvar(
    returns: pd.Series,
    confidence_level: float = VAR_CONFIDENCE_LEVEL,
) -> float:
    """Calculate the average daily loss beyond historical VaR."""
    clean_returns = returns.dropna()

    if clean_returns.empty:
        return float("nan")

    lower_quantile = clean_returns.quantile(
        1.0 - confidence_level
    )

    tail_returns = clean_returns[
        clean_returns <= lower_quantile
    ]

    if tail_returns.empty:
        return 0.0

    return float(max(0.0, -tail_returns.mean()))


def calculate_return_metrics(
    returns: pd.Series,
    annual_risk_free_rate: float,
) -> dict[str, float | int]:
    """Calculate annualised return and risk measures."""
    clean_returns = pd.to_numeric(
        returns,
        errors="coerce",
    ).dropna()

    observation_count = int(len(clean_returns))

    if observation_count < 2:
        return {
            "observations": observation_count,
            "annualized_return": float("nan"),
            "annualized_volatility": float("nan"),
            "sharpe_ratio": float("nan"),
            "sortino_ratio": float("nan"),
            "maximum_drawdown": float("nan"),
            "historical_var_95_daily": float("nan"),
            "historical_cvar_95_daily": float("nan"),
        }

    daily_mean_return = float(clean_returns.mean())
    daily_volatility = float(clean_returns.std(ddof=1))

    annualized_return = (
        daily_mean_return
        * TRADING_DAYS_PER_YEAR
    )

    annualized_volatility = (
        daily_volatility
        * sqrt(TRADING_DAYS_PER_YEAR)
    )

    if annualized_volatility > VALUE_ZERO_TOLERANCE:
        sharpe_ratio = (
            annualized_return
            - annual_risk_free_rate
        ) / annualized_volatility
    else:
        sharpe_ratio = float("nan")

    daily_risk_free_rate = (
        (1.0 + annual_risk_free_rate)
        ** (1.0 / TRADING_DAYS_PER_YEAR)
    ) - 1.0

    downside_returns = (
        clean_returns - daily_risk_free_rate
    )

    downside_returns = downside_returns[
        downside_returns < 0
    ]

    if len(downside_returns) >= 2:
        annualized_downside_deviation = (
            float(
                np.sqrt(
                    np.mean(
                        np.square(downside_returns)
                    )
                )
            )
            * sqrt(TRADING_DAYS_PER_YEAR)
        )
    else:
        annualized_downside_deviation = float("nan")

    if (
        pd.notna(annualized_downside_deviation)
        and annualized_downside_deviation
        > VALUE_ZERO_TOLERANCE
    ):
        sortino_ratio = (
            annualized_return
            - annual_risk_free_rate
        ) / annualized_downside_deviation
    else:
        sortino_ratio = float("nan")

    return {
        "observations": observation_count,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "maximum_drawdown": calculate_maximum_drawdown(
            clean_returns
        ),
        "historical_var_95_daily": calculate_historical_var(
            clean_returns
        ),
        "historical_cvar_95_daily": calculate_historical_cvar(
            clean_returns
        ),
    }


def build_security_metrics(
    security_returns: pd.DataFrame,
    annual_risk_free_rate: float,
) -> pd.DataFrame:
    """Calculate return and risk measures for each open security."""
    records = []

    for ticker in security_returns.columns:
        metrics = calculate_return_metrics(
            returns=security_returns[ticker],
            annual_risk_free_rate=annual_risk_free_rate,
        )

        records.append(
            {
                "ticker": ticker,
                **metrics,
            }
        )

    if not records:
        return pd.DataFrame(
            columns=[
                "ticker",
                "observations",
                "annualized_return",
                "annualized_volatility",
                "sharpe_ratio",
                "sortino_ratio",
                "maximum_drawdown",
                "historical_var_95_daily",
                "historical_cvar_95_daily",
            ]
        )

    return (
        pd.DataFrame(records)
        .sort_values(
            "ticker",
            kind="stable",
        )
        .reset_index(drop=True)
    )


def calculate_risk_contribution(
    security_returns: pd.DataFrame,
    holdings: pd.DataFrame,
) -> pd.DataFrame:
    """Calculate each open position's contribution to portfolio risk."""
    weight_series = (
        holdings
        .set_index("ticker")["weight"]
        .astype(float)
    )

    available_tickers = [
        ticker
        for ticker in weight_series.index
        if ticker in security_returns.columns
        and security_returns[ticker].count()
        >= MINIMUM_OBSERVATIONS
    ]

    if not available_tickers:
        return pd.DataFrame(
            columns=[
                "ticker",
                "weight",
                "marginal_risk",
                "component_risk",
                "risk_contribution_percent",
            ]
        )

    aligned_returns = security_returns[
        available_tickers
    ].dropna(how="any")

    if len(aligned_returns) < MINIMUM_OBSERVATIONS:
        return pd.DataFrame(
            columns=[
                "ticker",
                "weight",
                "marginal_risk",
                "component_risk",
                "risk_contribution_percent",
            ]
        )

    aligned_weights = weight_series.loc[
        available_tickers
    ].copy()

    aligned_weights = (
        aligned_weights
        / aligned_weights.sum()
    )

    annualized_covariance = (
        aligned_returns.cov()
        * TRADING_DAYS_PER_YEAR
    )

    weight_vector = aligned_weights.to_numpy(
        dtype=float
    )

    covariance_matrix = annualized_covariance.to_numpy(
        dtype=float
    )

    portfolio_variance = float(
        weight_vector.T
        @ covariance_matrix
        @ weight_vector
    )

    if portfolio_variance <= VALUE_ZERO_TOLERANCE:
        return pd.DataFrame(
            columns=[
                "ticker",
                "weight",
                "marginal_risk",
                "component_risk",
                "risk_contribution_percent",
            ]
        )

    portfolio_volatility = sqrt(portfolio_variance)

    marginal_risk = (
        covariance_matrix @ weight_vector
    ) / portfolio_volatility

    component_risk = (
        weight_vector * marginal_risk
    )

    risk_contribution_percent = (
        component_risk / portfolio_volatility
    )

    result = pd.DataFrame(
        {
            "ticker": available_tickers,
            "weight": weight_vector,
            "marginal_risk": marginal_risk,
            "component_risk": component_risk,
            "risk_contribution_percent":
                risk_contribution_percent,
        }
    )

    return (
        result
        .sort_values(
            "risk_contribution_percent",
            ascending=False,
            kind="stable",
        )
        .reset_index(drop=True)
    )


def build_risk_analysis(
    start_date: str | None = None,
    end_date: str | None = None,
    risk_free_rate: float | None = None,
) -> RiskAnalysisResult:
    """Build the complete portfolio risk analysis."""
    risk_free_details = resolve_risk_free_rate(
        risk_free_rate
    )

    annual_risk_free_rate = float(
        risk_free_details["rate"]
    )

    (
        portfolio_daily,
        previous_cumulative_twr,
    ) = load_portfolio_daily(
        start_date=start_date,
        end_date=end_date,
    )

    holdings = load_open_holdings()

    market_prices = load_market_prices(
        start_date=start_date,
        end_date=end_date,
    )

    open_tickers = holdings[
        "ticker"
    ].drop_duplicates().tolist()

    security_returns = create_security_return_matrix(
        market_prices=market_prices,
        tickers=open_tickers,
    )

    portfolio_returns = pd.to_numeric(
        portfolio_daily["daily_return"],
        errors="coerce",
    ).dropna()

    portfolio_metrics = calculate_return_metrics(
        returns=portfolio_returns,
        annual_risk_free_rate=annual_risk_free_rate,
    )

    security_metrics = build_security_metrics(
        security_returns=security_returns,
        annual_risk_free_rate=annual_risk_free_rate,
    )

    correlation_matrix = security_returns.corr(
        min_periods=MINIMUM_OBSERVATIONS
    )

    covariance_matrix = (
        security_returns.cov(
            min_periods=MINIMUM_OBSERVATIONS
        )
        * TRADING_DAYS_PER_YEAR
    )

    risk_contribution = calculate_risk_contribution(
        security_returns=security_returns,
        holdings=holdings,
    )

    cumulative_twr = calculate_period_twr(
        portfolio_daily=portfolio_daily,
        previous_cumulative_twr=previous_cumulative_twr,
    )

    summary = {
        "analysis_start_date":
            portfolio_daily["date"].min().strftime("%Y-%m-%d"),
        "analysis_end_date":
            portfolio_daily["date"].max().strftime("%Y-%m-%d"),
        "portfolio_observations":
            portfolio_metrics["observations"],
        "open_security_count":
            int(len(open_tickers)),
        "cumulative_portfolio_twr":
            cumulative_twr,
        "annualized_portfolio_return":
            portfolio_metrics["annualized_return"],
        "annualized_portfolio_volatility":
            portfolio_metrics["annualized_volatility"],
        "portfolio_sharpe_ratio":
            portfolio_metrics["sharpe_ratio"],
        "portfolio_sortino_ratio":
            portfolio_metrics["sortino_ratio"],
        "portfolio_maximum_drawdown":
            portfolio_metrics["maximum_drawdown"],
        "portfolio_historical_var_95_daily":
            portfolio_metrics["historical_var_95_daily"],
        "portfolio_historical_cvar_95_daily":
            portfolio_metrics["historical_cvar_95_daily"],
        "risk_free_rate":
            annual_risk_free_rate,
        "risk_free_rate_percent":
            risk_free_details["percentage"],
        "risk_free_rate_source":
            risk_free_details["source"],
        "risk_free_rate_observation_date":
            risk_free_details["observation_date"],
    }

    return RiskAnalysisResult(
        summary=summary,
        security_metrics=security_metrics,
        correlation_matrix=correlation_matrix,
        covariance_matrix=covariance_matrix,
        risk_contribution=risk_contribution,
    )


def create_argument_parser() -> ArgumentParser:
    """Create command-line arguments."""
    parser = ArgumentParser(
        description="Calculate portfolio risk measures."
    )

    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Optional analysis start date in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Optional analysis end date in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--risk-free-rate",
        type=float,
        default=None,
        help=(
            "Optional annual risk-free-rate override as a decimal. "
            "If omitted, Yahoo Finance ^TNX is used automatically. "
            "Example: 0.04 represents 4 percent."
        ),
    )

    return parser


def print_analysis_result(
    result: RiskAnalysisResult,
) -> None:
    """Print the risk analysis without changing stored precision."""
    summary = result.summary

    print("\nPORTFOLIO RISK ANALYSIS")
    print("=" * 70)

    print(
        f"Analysis period: "
        f"{summary['analysis_start_date']} to "
        f"{summary['analysis_end_date']}"
    )

    print(
        f"Portfolio observations: "
        f"{summary['portfolio_observations']}"
    )

    print(
        f"Open securities: "
        f"{summary['open_security_count']}"
    )

    print("\nRISK-FREE RATE")
    print("-" * 70)

    print(
        f"Source: "
        f"{summary['risk_free_rate_source']}"
    )

    print(
        f"Observation date: "
        f"{summary['risk_free_rate_observation_date']}"
    )

    print(
        f"Annual risk-free rate used: "
        f"{summary['risk_free_rate_percent']:.6f}%"
    )

    print(
        f"Decimal rate used in calculations: "
        f"{summary['risk_free_rate']}"
    )

    print("\nPORTFOLIO METRICS")
    print("-" * 70)

    print(
        f"Cumulative portfolio TWR: "
        f"{summary['cumulative_portfolio_twr']}"
    )

    print(
        f"Annualized return: "
        f"{summary['annualized_portfolio_return']}"
    )

    print(
        f"Annualized volatility: "
        f"{summary['annualized_portfolio_volatility']}"
    )

    print(
        f"Sharpe ratio: "
        f"{summary['portfolio_sharpe_ratio']}"
    )

    print(
        f"Sortino ratio: "
        f"{summary['portfolio_sortino_ratio']}"
    )

    print(
        f"Maximum drawdown: "
        f"{summary['portfolio_maximum_drawdown']}"
    )

    print(
        f"Historical daily VaR 95%: "
        f"{summary['portfolio_historical_var_95_daily']}"
    )

    print(
        f"Historical daily CVaR 95%: "
        f"{summary['portfolio_historical_cvar_95_daily']}"
    )

    with pd.option_context(
        "display.max_rows",
        None,
        "display.max_columns",
        None,
        "display.width",
        220,
        "display.float_format",
        lambda value: f"{value:.6f}",
    ):
        print("\nSECURITY RISK METRICS")
        print("-" * 70)
        print(result.security_metrics.to_string(index=False))

        print("\nRISK CONTRIBUTION")
        print("-" * 70)

        if result.risk_contribution.empty:
            print(
                "Risk contribution could not be calculated. "
                "At least 30 common return observations are required."
            )
        else:
            print(
                result.risk_contribution.to_string(
                    index=False
                )
            )

        print("\nCORRELATION MATRIX")
        print("-" * 70)
        print(result.correlation_matrix.to_string())

        print("\nANNUALIZED COVARIANCE MATRIX")
        print("-" * 70)
        print(result.covariance_matrix.to_string())


def main() -> None:
    """Run the complete portfolio risk analysis."""
    arguments = create_argument_parser().parse_args()

    validate_database()

    result = build_risk_analysis(
        start_date=arguments.start_date,
        end_date=arguments.end_date,
        risk_free_rate=arguments.risk_free_rate,
    )

    print_analysis_result(result)


if __name__ == "__main__":
    main()
