from typing import Any

import numpy as np
import pandas as pd

from database import (
    database_transaction,
    fetch_all,
    fetch_one,
    validate_database,
)


VALUE_ZERO_TOLERANCE = 1e-10
RECONCILIATION_TOLERANCE = 1e-6


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


def load_cash_movements() -> pd.DataFrame:
    """
    Load deposits, withdrawals and interest income.
    """

    rows = fetch_all(
        """
        SELECT
            cash_id,
            date,
            time,
            type,
            amount_usd
        FROM cash_movements
        ORDER BY
            date,
            COALESCE(time, '00:00:00'),
            cash_id
        """
    )

    cash_movements = pd.DataFrame(rows)

    if cash_movements.empty:
        return pd.DataFrame(
            columns=[
                "cash_id",
                "date",
                "time",
                "type",
                "amount_usd",
            ]
        )

    cash_movements["date"] = pd.to_datetime(
        cash_movements["date"],
        errors="raise",
    ).dt.normalize()

    cash_movements["type"] = (
        cash_movements["type"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    cash_movements["amount_usd"] = pd.to_numeric(
        cash_movements["amount_usd"],
        errors="raise",
    )

    return cash_movements


def load_dividends() -> pd.DataFrame:
    """
    Load net dividend income.
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
        ORDER BY date, dividend_id
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


def load_market_prices() -> pd.DataFrame:
    """
    Load real closing prices from SQLite.
    """

    rows = fetch_all(
        """
        SELECT
            ticker,
            date,
            close_usd
        FROM market_prices
        WHERE ticker <> 'SPY'
        ORDER BY ticker, date
        """
    )

    prices = pd.DataFrame(rows)

    if prices.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "date",
                "close_usd",
            ]
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

    prices["close_usd"] = pd.to_numeric(
        prices["close_usd"],
        errors="raise",
    )

    return prices


def build_portfolio_calendar(
    trades: pd.DataFrame,
    cash_movements: pd.DataFrame,
    dividends: pd.DataFrame,
    market_prices: pd.DataFrame,
) -> pd.DatetimeIndex:
    """
    Build the portfolio calendar from the first real portfolio event.

    Historical prices before portfolio inception are intentionally
    excluded from the portfolio performance calendar.
    """

    event_dates: set[pd.Timestamp] = set(
        trades["date"].tolist()
    )

    if not cash_movements.empty:
        event_dates.update(
            cash_movements["date"].tolist()
        )

    if not dividends.empty:
        event_dates.update(
            dividends["date"].tolist()
        )

    if not event_dates:
        raise RuntimeError(
            "No portfolio events were found."
        )

    # Portfolio performance begins with the first real portfolio event.
    start_date = min(event_dates)

    # The portfolio must include every transaction and cash-flow date.
    end_date = max(event_dates)

    # Open positions may have market prices after the latest event.
    if not market_prices.empty:
        end_date = max(
            end_date,
            market_prices["date"].max(),
        )

    business_dates = pd.bdate_range(
        start=start_date,
        end=end_date,
    )

    # Preserve portfolio events that occur outside normal business days.
    calendar = business_dates.union(
        pd.DatetimeIndex(
            sorted(event_dates)
        )
    )

    return calendar.sort_values()

def build_daily_share_matrix(
    trades: pd.DataFrame,
    calendar: pd.DatetimeIndex,
) -> pd.DataFrame:
    """
    Calculate end-of-day shares for every ticker.
    """

    share_changes = trades[
        [
            "date",
            "ticker",
            "side",
            "shares",
        ]
    ].copy()

    share_changes["share_change"] = (
        share_changes["shares"]
    )

    sell_rows = (
        share_changes["side"] == "Sell"
    )

    share_changes.loc[
        sell_rows,
        "share_change",
    ] = -share_changes.loc[
        sell_rows,
        "shares",
    ]

    daily_changes = (
        share_changes
        .groupby(
            ["date", "ticker"],
            as_index=False,
        )["share_change"]
        .sum()
        .pivot(
            index="date",
            columns="ticker",
            values="share_change",
        )
    )

    tickers = sorted(
        trades["ticker"].unique()
    )

    daily_changes = (
        daily_changes
        .reindex(
            index=calendar,
            columns=tickers,
        )
        .fillna(0.0)
    )

    daily_shares = daily_changes.cumsum()

    daily_shares = daily_shares.mask(
        daily_shares.abs()
        <= VALUE_ZERO_TOLERANCE,
        0.0,
    )

    return daily_shares


def build_real_price_matrix(
    market_prices: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    tickers: list[str],
) -> pd.DataFrame:
    """
    Build a daily matrix of real closing prices.
    """

    price_matrix = pd.DataFrame(
        index=calendar,
        columns=tickers,
        dtype=float,
    )

    if market_prices.empty:
        return price_matrix

    real_prices = market_prices.pivot(
        index="date",
        columns="ticker",
        values="close_usd",
    )

    available_tickers = [
        ticker
        for ticker in tickers
        if ticker in real_prices.columns
    ]

    if available_tickers:
        reindexed_prices = (
            real_prices[
                available_tickers
            ]
            .reindex(calendar)
            .ffill()
        )

        price_matrix.loc[
            :,
            available_tickers,
        ] = reindexed_prices

    return price_matrix


def build_trade_fallback_series(
    ticker_trades: pd.DataFrame,
    calendar: pd.DatetimeIndex,
) -> pd.Series:
    """
    Estimate delisted ticker prices between trade-price anchors.

    Geometric interpolation preserves the observed trade-price
    endpoints and distributes the return across intervening dates.
    """

    ordered_trades = (
        ticker_trades
        .sort_values(
            by=[
                "date",
                "time",
                "trade_id",
            ],
            kind="stable",
        )
        .copy()
    )

    daily_anchors = (
        ordered_trades
        .groupby(
            "date",
            as_index=True,
        )["price_usd"]
        .last()
        .sort_index()
    )

    fallback_series = pd.Series(
        np.nan,
        index=calendar,
        dtype=float,
    )

    anchor_dates = [
        anchor_date
        for anchor_date in daily_anchors.index
        if anchor_date in calendar
    ]

    if not anchor_dates:
        return fallback_series

    if len(anchor_dates) == 1:
        fallback_series.loc[
            anchor_dates[0]
        ] = float(
            daily_anchors.loc[
                anchor_dates[0]
            ]
        )

        return fallback_series

    for start_date, end_date in zip(
        anchor_dates[:-1],
        anchor_dates[1:],
    ):
        start_price = float(
            daily_anchors.loc[start_date]
        )

        end_price = float(
            daily_anchors.loc[end_date]
        )

        segment_dates = calendar[
            (calendar >= start_date)
            & (calendar <= end_date)
        ]

        interval_count = (
            len(segment_dates) - 1
        )

        if interval_count <= 0:
            fallback_series.loc[
                start_date
            ] = start_price

            continue

        if start_price > 0 and end_price > 0:
            progress = (
                np.arange(
                    len(segment_dates)
                )
                / interval_count
            )

            estimated_prices = (
                start_price
                * (
                    end_price
                    / start_price
                )
                ** progress
            )

        else:
            estimated_prices = np.linspace(
                start=start_price,
                stop=end_price,
                num=len(segment_dates),
            )

        fallback_series.loc[
            segment_dates
        ] = estimated_prices

    return fallback_series


def complete_price_matrix(
    price_matrix: pd.DataFrame,
    trades: pd.DataFrame,
    daily_shares: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Add trade-based fallback prices only where real prices are absent.
    """

    completed_prices = price_matrix.copy()
    fallback_tickers: list[str] = []

    for ticker in daily_shares.columns:
        ticker_has_real_prices = (
            completed_prices[ticker]
            .notna()
            .any()
        )

        if ticker_has_real_prices:
            continue

        ticker_trades = trades[
            trades["ticker"] == ticker
        ].copy()

        fallback_prices = (
            build_trade_fallback_series(
                ticker_trades=ticker_trades,
                calendar=completed_prices.index,
            )
        )

        completed_prices[ticker] = (
            fallback_prices
        )

        fallback_tickers.append(ticker)

    return completed_prices, fallback_tickers


def validate_price_coverage(
    daily_shares: pd.DataFrame,
    price_matrix: pd.DataFrame,
) -> None:
    """
    Ensure every non-zero position has a price.
    """

    active_positions = (
        daily_shares.abs()
        > VALUE_ZERO_TOLERANCE
    )

    missing_required_prices = (
        active_positions
        & price_matrix.isna()
    )

    if missing_required_prices.any().any():
        problems: list[str] = []

        for ticker in missing_required_prices.columns:
            missing_dates = (
                missing_required_prices.index[
                    missing_required_prices[ticker]
                ]
            )

            if len(missing_dates) > 0:
                problems.append(
                    f"{ticker}: "
                    f"{missing_dates[0].date()}"
                )

        raise RuntimeError(
            "Prices are missing while positions are open: "
            + ", ".join(problems)
        )


def build_daily_cash_data(
    trades: pd.DataFrame,
    cash_movements: pd.DataFrame,
    dividends: pd.DataFrame,
    calendar: pd.DatetimeIndex,
) -> pd.DataFrame:
    """
    Calculate daily cash balance and external cash flows.
    """

    cash_data = pd.DataFrame(
        0.0,
        index=calendar,
        columns=[
            "cash_change",
            "external_inflow_usd",
            "external_outflow_usd",
        ],
    )

    trade_cash = (
        trades
        .groupby("date")["net_flow_usd"]
        .sum()
    )

    cash_data.loc[
        trade_cash.index,
        "cash_change",
    ] += trade_cash

    if not dividends.empty:
        dividend_cash = (
            dividends
            .groupby("date")[
                "net_dividend_usd"
            ]
            .sum()
        )

        cash_data.loc[
            dividend_cash.index,
            "cash_change",
        ] += dividend_cash

    if not cash_movements.empty:
        for movement in (
            cash_movements.itertuples(
                index=False
            )
        ):
            movement_date = movement.date
            amount = float(
                movement.amount_usd
            )

            if movement.type == "DEPOSIT":
                cash_data.loc[
                    movement_date,
                    "cash_change",
                ] += amount

                cash_data.loc[
                    movement_date,
                    "external_inflow_usd",
                ] += amount

            elif movement.type == "WITHDRAWAL":
                cash_data.loc[
                    movement_date,
                    "cash_change",
                ] -= amount

                cash_data.loc[
                    movement_date,
                    "external_outflow_usd",
                ] += amount

            elif movement.type == "INTEREST":
                cash_data.loc[
                    movement_date,
                    "cash_change",
                ] += amount

            else:
                raise ValueError(
                    f"Unknown cash movement type: "
                    f"{movement.type}"
                )

    gift_trades = trades[
        (trades["side"] == "Buy")
        & (
            trades["net_flow_usd"].abs()
            <= VALUE_ZERO_TOLERANCE
        )
    ].copy()

    if not gift_trades.empty:
        gift_trades["gift_value_usd"] = (
            gift_trades["shares"]
            * gift_trades["price_usd"]
        )

        gift_inflows = (
            gift_trades
            .groupby("date")[
                "gift_value_usd"
            ]
            .sum()
        )

        # Gift shares are non-cash external contributions.
        cash_data.loc[
            gift_inflows.index,
            "external_inflow_usd",
        ] += gift_inflows

    cash_data["cash_balance_usd"] = (
        cash_data["cash_change"]
        .cumsum()
    )

    return cash_data


def calculate_daily_returns(
    daily_table: pd.DataFrame,
) -> pd.Series:
    """
    Calculate daily TWR returns using beginning-of-day flow timing.
    """

    returns: list[float] = []

    for row in daily_table.itertuples():
        net_external_flow = (
            row.external_inflow_usd
            - row.external_outflow_usd
        )

        invested_capital = (
            row.opening_value_usd
            + net_external_flow
        )

        if (
            abs(invested_capital)
            <= VALUE_ZERO_TOLERANCE
        ):
            if (
                abs(row.closing_value_usd)
                <= VALUE_ZERO_TOLERANCE
            ):
                daily_return = 0.0

            else:
                daily_return = 0.0

        else:
            daily_return = (
                row.closing_value_usd
                / invested_capital
                - 1
            )

        returns.append(
            float(daily_return)
        )

    return pd.Series(
        returns,
        index=daily_table.index,
        dtype=float,
    )


def build_portfolio_daily() -> tuple[
    pd.DataFrame,
    dict[str, Any],
]:
    """
    Build daily portfolio valuation and TWR data.
    """

    trades = load_trades()
    cash_movements = load_cash_movements()
    dividends = load_dividends()
    market_prices = load_market_prices()

    calendar = build_portfolio_calendar(
        trades=trades,
        cash_movements=cash_movements,
        dividends=dividends,
        market_prices=market_prices,
    )

    daily_shares = build_daily_share_matrix(
        trades=trades,
        calendar=calendar,
    )

    price_matrix = build_real_price_matrix(
        market_prices=market_prices,
        calendar=calendar,
        tickers=daily_shares.columns.tolist(),
    )

    (
        price_matrix,
        fallback_tickers,
    ) = complete_price_matrix(
        price_matrix=price_matrix,
        trades=trades,
        daily_shares=daily_shares,
    )

    validate_price_coverage(
        daily_shares=daily_shares,
        price_matrix=price_matrix,
    )

    daily_security_values = (
        daily_shares
        * price_matrix.fillna(0.0)
    )

    securities_value = (
        daily_security_values.sum(axis=1)
    )

    cash_data = build_daily_cash_data(
        trades=trades,
        cash_movements=cash_movements,
        dividends=dividends,
        calendar=calendar,
    )

    portfolio_daily = pd.DataFrame(
        index=calendar
    )

    portfolio_daily[
        "securities_value_usd"
    ] = securities_value

    portfolio_daily[
        "cash_balance_usd"
    ] = cash_data["cash_balance_usd"]

    portfolio_daily[
        "closing_value_usd"
    ] = (
        portfolio_daily[
            "securities_value_usd"
        ]
        + portfolio_daily[
            "cash_balance_usd"
        ]
    )

    portfolio_daily[
        "opening_value_usd"
    ] = (
        portfolio_daily[
            "closing_value_usd"
        ]
        .shift(1)
        .fillna(0.0)
    )

    portfolio_daily[
        "external_inflow_usd"
    ] = cash_data[
        "external_inflow_usd"
    ]

    portfolio_daily[
        "external_outflow_usd"
    ] = cash_data[
        "external_outflow_usd"
    ]

    portfolio_daily["daily_return"] = (
        calculate_daily_returns(
            portfolio_daily
        )
    )

    portfolio_daily["cumulative_twr"] = (
        (
            1.0
            + portfolio_daily[
                "daily_return"
            ]
        )
        .cumprod()
        .sub(1.0)
    )

    cumulative_twr = float(
        portfolio_daily[
            "cumulative_twr"
        ].iloc[-1]
    )

    portfolio_daily = (
        portfolio_daily.reset_index()
        .rename(
            columns={
                "index": "date",
            }
        )
    )

    portfolio_daily["date"] = (
        portfolio_daily["date"]
        .dt.strftime("%Y-%m-%d")
    )

    final_row = portfolio_daily.iloc[-1]

    expected_row = fetch_one(
        """
        SELECT
            COALESCE(
                SUM(market_value_usd),
                0
            ) AS expected_portfolio_value
        FROM holdings_summary
        """
    )

    expected_portfolio_value = float(
        expected_row[
            "expected_portfolio_value"
        ]
    )

    reconciliation_difference = (
        float(
            final_row[
                "closing_value_usd"
            ]
        )
        - expected_portfolio_value
    )

    metadata = {
        "cumulative_twr": float(
            cumulative_twr
        ),
        "fallback_tickers": (
            fallback_tickers
        ),
        "final_portfolio_value": float(
            final_row[
                "closing_value_usd"
            ]
        ),
        "expected_portfolio_value": (
            expected_portfolio_value
        ),
        "reconciliation_difference": float(
            reconciliation_difference
        ),
        "is_reconciled": (
            abs(reconciliation_difference)
            <= RECONCILIATION_TOLERANCE
        ),
    }

    return portfolio_daily, metadata


def save_portfolio_daily(
    portfolio_daily: pd.DataFrame,
) -> int:
    """
    Replace the portfolio_daily table with calculated rows.
    """

    query = """
        INSERT INTO portfolio_daily (
            date,
            opening_value_usd,
            securities_value_usd,
            cash_balance_usd,
            closing_value_usd,
            external_inflow_usd,
            external_outflow_usd,
            daily_return,
            cumulative_twr
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    rows = [
        (
            row.date,
            float(row.opening_value_usd),
            float(row.securities_value_usd),
            float(row.cash_balance_usd),
            float(row.closing_value_usd),
            float(row.external_inflow_usd),
            float(row.external_outflow_usd),
            float(row.daily_return),
            float(row.cumulative_twr),
        )
        for row in portfolio_daily.itertuples(
            index=False
        )
    ]

    with database_transaction() as connection:
        connection.execute(
            "DELETE FROM portfolio_daily"
        )

        connection.executemany(
            query,
            rows,
        )

    return len(rows)


def refresh_portfolio_daily() -> dict[str, Any]:
    """
    Calculate, validate and save the daily portfolio table.
    """

    validate_database()

    portfolio_daily, metadata = (
        build_portfolio_daily()
    )

    saved_rows = save_portfolio_daily(
        portfolio_daily
    )

    return {
        "saved_rows": saved_rows,
        **metadata,
    }


def main() -> None:
    """
    Refresh portfolio_daily and print summary information.
    """

    result = refresh_portfolio_daily()

    print(
        "Portfolio daily table refreshed successfully."
    )

    print(
        f"Rows saved: {result['saved_rows']}"
    )

    print(
        f"Cumulative portfolio TWR: "
        f"{result['cumulative_twr']}"
    )

    print(
        f"Final calculated value: "
        f"{result['final_portfolio_value']}"
    )

    print(
        f"Holdings summary value: "
        f"{result['expected_portfolio_value']}"
    )

    print(
        f"Reconciliation difference: "
        f"{result['reconciliation_difference']}"
    )

    print(
        f"Reconciled: "
        f"{result['is_reconciled']}"
    )

    if result["fallback_tickers"]:
        print(
            "Trade-price fallback tickers: "
            + ", ".join(
                result["fallback_tickers"]
            )
        )


if __name__ == "__main__":
    main()
