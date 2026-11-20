from math import sqrt
from typing import Any

import numpy as np
import pandas as pd

from database import (
    fetch_all,
    validate_database,
)


TRADING_DAYS_PER_YEAR = 252
VALUE_ZERO_TOLERANCE = 1e-10
DEFAULT_RISK_FREE_RATE = 0.0


def load_trades() -> pd.DataFrame:
    """
    Load all trades in chronological order.
    """

    rows = fetch_all(
        """
        SELECT
            trade_id,
            date,
            time,
            ticker,
            side,
            shares,
            price_usd,
            commission_usd,
            net_flow_usd,
            cumulative_shares
        FROM trades
        ORDER BY
            ticker,
            date,
            COALESCE(time, '00:00:00'),
            trade_id
        """
    )

    trades = pd.DataFrame(rows)

    if trades.empty:
        raise RuntimeError(
            "No trades were found."
        )

    trades["date"] = pd.to_datetime(
        trades["date"],
        errors="raise",
    ).dt.normalize()

    trades["time"] = trades["time"].fillna(
        "00:00:00"
    )

    trades["ticker"] = (
        trades["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    numeric_columns = [
        "shares",
        "price_usd",
        "commission_usd",
        "net_flow_usd",
        "cumulative_shares",
    ]

    for column in numeric_columns:
        trades[column] = pd.to_numeric(
            trades[column],
            errors="raise",
        )

    return trades


def load_dividends() -> pd.DataFrame:
    """
    Load net dividends.
    """

    rows = fetch_all(
        """
        SELECT
            dividend_id,
            date,
            time,
            ticker,
            net_dividend_usd
        FROM dividends
        ORDER BY
            ticker,
            date,
            dividend_id
        """
    )

    dividends = pd.DataFrame(rows)

    if dividends.empty:
        return pd.DataFrame(
            columns=[
                "dividend_id",
                "date",
                "time",
                "ticker",
                "net_dividend_usd",
            ]
        )

    dividends["date"] = pd.to_datetime(
        dividends["date"],
        errors="raise",
    ).dt.normalize()

    dividends["ticker"] = (
        dividends["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    dividends["net_dividend_usd"] = (
        pd.to_numeric(
            dividends["net_dividend_usd"],
            errors="raise",
        )
    )

    return dividends


def load_holdings_summary() -> pd.DataFrame:
    """
    Load calculated holdings and current prices.
    """

    rows = fetch_all(
        """
        SELECT
            ticker,
            as_of_date,
            status,
            shares,
            price_usd,
            market_value_usd,
            weight,
            realized_pnl_usd,
            unrealized_pnl_usd
        FROM holdings_summary
        WHERE ticker <> 'CASH'
        ORDER BY ticker
        """
    )

    holdings = pd.DataFrame(rows)

    if holdings.empty:
        raise RuntimeError(
            "Holdings summary is empty. "
            "Run portfolio_calculator.py first."
        )

    holdings["ticker"] = (
        holdings["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    holdings["as_of_date"] = pd.to_datetime(
        holdings["as_of_date"],
        errors="raise",
    ).dt.normalize()

    numeric_columns = [
        "shares",
        "price_usd",
        "market_value_usd",
        "weight",
        "realized_pnl_usd",
        "unrealized_pnl_usd",
    ]

    for column in numeric_columns:
        holdings[column] = pd.to_numeric(
            holdings[column],
            errors="raise",
        )

    return holdings


def load_adjusted_prices() -> pd.DataFrame:
    """
    Load adjusted closing prices for open securities and benchmarks.
    """

    rows = fetch_all(
        """
        SELECT
            ticker,
            date,
            adjusted_close_usd
        FROM market_prices
        ORDER BY ticker, date
        """
    )

    prices = pd.DataFrame(rows)

    if prices.empty:
        raise RuntimeError(
            "Market prices are empty. "
            "Run market_data.py first."
        )

    prices["date"] = pd.to_datetime(
        prices["date"],
        errors="raise",
    ).dt.normalize()

    prices["ticker"] = (
        prices["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    prices["adjusted_close_usd"] = (
        pd.to_numeric(
            prices["adjusted_close_usd"],
            errors="raise",
        )
    )

    return prices


def get_dividends_between(
    dividends: pd.DataFrame,
    start_date: pd.Timestamp | None,
    end_date: pd.Timestamp,
) -> float:
    """
    Return dividends after start_date and through end_date.
    """

    if dividends.empty:
        return 0.0

    mask = dividends["date"] <= end_date

    if start_date is not None:
        mask &= dividends["date"] > start_date

    return float(
        dividends.loc[
            mask,
            "net_dividend_usd",
        ].sum()
    )


def calculate_ticker_twr(
    ticker_trades: pd.DataFrame,
    ticker_dividends: pd.DataFrame,
    holding_row: pd.Series,
) -> dict[str, Any]:
    """
    Calculate transaction-event-based TWR for one security.
    """

    ticker = ticker_trades["ticker"].iloc[0]

    ticker_trades = (
        ticker_trades
        .sort_values(
            by=[
                "date",
                "time",
                "trade_id",
            ],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    current_shares = 0.0
    previous_value_after_event: float | None = None
    previous_event_date: pd.Timestamp | None = None

    linked_growth_factor = 1.0
    calculated_subperiods = 0

    for row_index, trade in (
        ticker_trades.iterrows()
    ):
        trade_date = trade["date"]
        trade_price = float(
            trade["price_usd"]
        )
        trade_shares = float(
            trade["shares"]
        )
        net_flow_usd = float(
            trade["net_flow_usd"]
        )
        side = trade["side"]

        dividend_income = get_dividends_between(
            dividends=ticker_dividends,
            start_date=previous_event_date,
            end_date=trade_date,
        )

        if side == "Buy":
            shares_after_event = (
                current_shares
                + trade_shares
            )

            if (
                abs(net_flow_usd)
                <= VALUE_ZERO_TOLERANCE
            ):
                # Gift shares are treated as an in-kind
                # external contribution for return calculation.
                external_flow_usd = (
                    trade_shares
                    * trade_price
                )

            else:
                external_flow_usd = -net_flow_usd

        elif side == "Sell":
            shares_after_event = (
                current_shares
                - trade_shares
            )

            external_flow_usd = -net_flow_usd

        else:
            raise ValueError(
                f"Unknown side for {ticker}: {side}"
            )

        value_after_event = (
            shares_after_event
            * trade_price
        )

        if (
            previous_value_after_event is None
            or abs(previous_value_after_event)
            <= VALUE_ZERO_TOLERANCE
        ):
            if external_flow_usd > 0:
                subperiod_return = (
                    value_after_event
                    / external_flow_usd
                    - 1
                )

                linked_growth_factor *= (
                    1 + subperiod_return
                )

                calculated_subperiods += 1

        else:
            subperiod_return = (
                (
                    value_after_event
                    - external_flow_usd
                    + dividend_income
                )
                / previous_value_after_event
                - 1
            )

            linked_growth_factor *= (
                1 + subperiod_return
            )

            calculated_subperiods += 1

        current_shares = shares_after_event

        if (
            abs(current_shares)
            <= VALUE_ZERO_TOLERANCE
        ):
            current_shares = 0.0
            value_after_event = 0.0

        previous_value_after_event = (
            value_after_event
        )

        previous_event_date = trade_date

    status = holding_row["status"]

    if status == "OPEN":
        final_price = float(
            holding_row["price_usd"]
        )

        final_date = holding_row[
            "as_of_date"
        ]

        final_value = (
            current_shares
            * final_price
        )

        remaining_dividends = (
            get_dividends_between(
                dividends=ticker_dividends,
                start_date=previous_event_date,
                end_date=final_date,
            )
        )

        if (
            previous_value_after_event is not None
            and abs(previous_value_after_event)
            > VALUE_ZERO_TOLERANCE
        ):
            final_subperiod_return = (
                (
                    final_value
                    + remaining_dividends
                )
                / previous_value_after_event
                - 1
            )

            linked_growth_factor *= (
                1 + final_subperiod_return
            )

            calculated_subperiods += 1

        performance_end_date = final_date

    else:
        final_price = 0.0
        final_value = 0.0
        performance_end_date = (
            ticker_trades["date"].max()
        )

    total_dividends_usd = float(
        ticker_dividends[
            "net_dividend_usd"
        ].sum()
    ) if not ticker_dividends.empty else 0.0

    twr = (
        linked_growth_factor - 1
        if calculated_subperiods > 0
        else np.nan
    )

    return {
        "ticker": ticker,
        "status": status,
        "start_date": (
            ticker_trades["date"].min()
        ),
        "end_date": performance_end_date,
        "trade_count": len(ticker_trades),
        "subperiod_count": (
            calculated_subperiods
        ),
        "total_dividends_usd": (
            total_dividends_usd
        ),
        "security_twr": twr,
        "current_value_usd": final_value,
        "realized_pnl_usd": float(
            holding_row["realized_pnl_usd"]
        ),
        "unrealized_pnl_usd": float(
            holding_row["unrealized_pnl_usd"]
        ),
    }


def calculate_all_security_twr() -> pd.DataFrame:
    """
    Load the final security TWR results from security_daily.

    security_daily is the single source of truth for
    daily and cumulative security TWR.
    """

    rows = fetch_all(
        """
        WITH daily_summary AS (
            SELECT
                ticker,
                MIN(date) AS start_date,
                MAX(date) AS end_date,
                COUNT(*) AS daily_observations
            FROM security_daily
            GROUP BY ticker
        ),

        latest_daily AS (
            SELECT
                ticker,
                date,
                closing_value_usd,
                cumulative_twr,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY date DESC
                ) AS row_number
            FROM security_daily
        ),

        trade_summary AS (
            SELECT
                ticker,
                COUNT(*) AS trade_count
            FROM trades
            GROUP BY ticker
        ),

        dividend_summary AS (
            SELECT
                ticker,
                SUM(
                    net_dividend_usd
                ) AS total_dividends_usd
            FROM dividends
            GROUP BY ticker
        )

        SELECT
            h.ticker,
            h.status,
            d.start_date,
            d.end_date,
            COALESCE(
                t.trade_count,
                0
            ) AS trade_count,
            d.daily_observations,
            COALESCE(
                v.total_dividends_usd,
                0
            ) AS total_dividends_usd,
            l.cumulative_twr
                AS security_twr,
            l.closing_value_usd
                AS current_value_usd,
            h.realized_pnl_usd,
            h.unrealized_pnl_usd

        FROM holdings_summary AS h

        INNER JOIN daily_summary AS d
          ON d.ticker = h.ticker

        INNER JOIN latest_daily AS l
          ON l.ticker = h.ticker
         AND l.row_number = 1

        LEFT JOIN trade_summary AS t
          ON t.ticker = h.ticker

        LEFT JOIN dividend_summary AS v
          ON v.ticker = h.ticker

        WHERE h.ticker <> 'CASH'

        ORDER BY
            CASE h.status
                WHEN 'OPEN' THEN 0
                WHEN 'CLOSED' THEN 1
                ELSE 2
            END,
            h.ticker
        """
    )

    performance = pd.DataFrame(rows)

    if performance.empty:
        raise RuntimeError(
            "Security daily performance is empty. "
            "Run security_daily_calculator.py first."
        )

    performance["start_date"] = (
        pd.to_datetime(
            performance["start_date"],
            errors="raise",
        )
        .dt.normalize()
    )

    performance["end_date"] = (
        pd.to_datetime(
            performance["end_date"],
            errors="raise",
        )
        .dt.normalize()
    )

    integer_columns = [
        "trade_count",
        "daily_observations",
    ]

    for column in integer_columns:
        performance[column] = (
            pd.to_numeric(
                performance[column],
                errors="raise",
            )
            .astype(int)
        )

    numeric_columns = [
        "total_dividends_usd",
        "security_twr",
        "current_value_usd",
        "realized_pnl_usd",
        "unrealized_pnl_usd",
    ]

    for column in numeric_columns:
        performance[column] = (
            pd.to_numeric(
                performance[column],
                errors="raise",
            )
        )

    return performance

def calculate_maximum_drawdown(
    prices: pd.Series,
) -> float:
    """
    Calculate maximum drawdown from a price series.
    """

    running_maximum = prices.cummax()

    drawdowns = (
        prices / running_maximum - 1
    )

    return float(drawdowns.min())


def calculate_market_metrics(
    adjusted_prices: pd.Series,
    risk_free_rate: float,
) -> dict[str, float]:
    """
    Calculate annualized risk metrics from adjusted prices.
    """

    prices = (
        adjusted_prices
        .dropna()
        .sort_index()
    )

    daily_returns = prices.pct_change(
        fill_method=None
    ).dropna()

    if len(prices) < 2 or daily_returns.empty:
        return {
            "annualized_return": np.nan,
            "annualized_volatility": np.nan,
            "sharpe_ratio": np.nan,
            "maximum_drawdown": np.nan,
            "price_observations": len(prices),
        }

    observation_count = len(daily_returns)

    growth_factor = (
        prices.iloc[-1]
        / prices.iloc[0]
    )

    if growth_factor > 0:
        annualized_return = (
            growth_factor
            ** (
                TRADING_DAYS_PER_YEAR
                / observation_count
            )
            - 1
        )

    else:
        annualized_return = np.nan

    annualized_volatility = (
        daily_returns.std(ddof=1)
        * sqrt(TRADING_DAYS_PER_YEAR)
    )

    if annualized_volatility > 0:
        sharpe_ratio = (
            annualized_return
            - risk_free_rate
        ) / annualized_volatility

    else:
        sharpe_ratio = np.nan

    maximum_drawdown = (
        calculate_maximum_drawdown(prices)
    )

    return {
        "annualized_return": float(
            annualized_return
        ),
        "annualized_volatility": float(
            annualized_volatility
        ),
        "sharpe_ratio": float(
            sharpe_ratio
        ),
        "maximum_drawdown": float(
            maximum_drawdown
        ),
        "price_observations": len(prices),
    }
def calculate_all_market_metrics(
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> pd.DataFrame:
    """
    Calculate market metrics for downloaded tickers.
    """

    prices = load_adjusted_prices()

    price_matrix = prices.pivot(
        index="date",
        columns="ticker",
        values="adjusted_close_usd",
    )

    results: list[dict[str, Any]] = []

    for ticker in price_matrix.columns:
        metrics = calculate_market_metrics(
            adjusted_prices=price_matrix[ticker],
            risk_free_rate=risk_free_rate,
        )

        results.append(
            {
                "ticker": ticker,
                **metrics,
            }
        )

    return pd.DataFrame(results)


def build_security_performance_table(
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> pd.DataFrame:
    """
    Combine transaction TWR with market-price metrics.
    """

    security_twr = calculate_all_security_twr()

    market_metrics = calculate_all_market_metrics(
        risk_free_rate=risk_free_rate
    )

    performance = security_twr.merge(
        market_metrics,
        on="ticker",
        how="outer",
    )

    performance["status"] = (
        performance["status"]
        .fillna("BENCHMARK")
    )

    status_order = {
        "OPEN": 0,
        "CLOSED": 1,
        "BENCHMARK": 2,
    }

    performance["_status_order"] = (
        performance["status"]
        .map(status_order)
        .fillna(3)
    )

    performance = (
        performance
        .sort_values(
            by=[
                "_status_order",
                "ticker",
            ],
            kind="stable",
        )
        .drop(columns="_status_order")
        .reset_index(drop=True)
    )

    return performance


def main() -> None:
    """
    Calculate and display security performance.
    """

    validate_database()

    performance = (
        build_security_performance_table()
    )

    print("Security performance calculated successfully.")
    print()

    with pd.option_context(
        "display.max_rows",
        None,
        "display.max_columns",
        None,
        "display.width",
        220,
        "display.float_format",
        lambda value: f"{value:.10f}",
    ):
        print(performance.to_string(index=False))


if __name__ == "__main__":
    main()
