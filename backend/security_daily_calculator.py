from typing import Any

import numpy as np
import pandas as pd

from database import (
    database_transaction,
    fetch_all,
    validate_database,
)
from performance_calculator import (
    load_holdings_summary,
)
from portfolio_daily_calculator import (
    build_daily_share_matrix,
    build_real_price_matrix,
    complete_price_matrix,
    load_dividends,
    load_market_prices,
    load_trades,
    validate_price_coverage,
)


VALUE_ZERO_TOLERANCE = 1e-10
SHARE_VALIDATION_TOLERANCE = 1e-8
RETURN_VALIDATION_TOLERANCE = 1e-12


def load_portfolio_calendar() -> pd.DatetimeIndex:
    """
    Load the validated portfolio calendar.

    The portfolio_daily table is used as the common calendar
    because it already contains business dates and portfolio
    event dates.
    """

    rows = fetch_all(
        """
        SELECT date
        FROM portfolio_daily
        ORDER BY date
        """
    )

    if not rows:
        raise RuntimeError(
            "The portfolio_daily table is empty. "
            "Run portfolio_daily_calculator.py first."
        )

    calendar = pd.DatetimeIndex(
        pd.to_datetime(
            [row["date"] for row in rows],
            errors="raise",
        )
    )

    calendar = (
        calendar
        .normalize()
        .unique()
        .sort_values()
    )

    return calendar


def normalize_trade_order(
    trades: pd.DataFrame,
) -> pd.DataFrame:
    """
    Ensure trades have a sortable time value.
    """

    normalized = trades.copy()

    normalized["time"] = (
        normalized["time"]
        .fillna("00:00:00")
        .astype(str)
    )

    normalized = (
        normalized
        .sort_values(
            by=[
                "ticker",
                "date",
                "time",
                "trade_id",
            ],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    return normalized


def calculate_external_trade_flow(
    trade: pd.Series,
) -> float:
    """
    Convert the database trade cash-flow sign into the
    security-level external-flow sign.

    Positive values represent capital entering the security.
    Negative values represent capital leaving the security.
    """

    side = str(trade["side"])
    shares = float(trade["shares"])
    price_usd = float(trade["price_usd"])
    net_flow_usd = float(
        trade["net_flow_usd"]
    )

    if side == "Buy":
        if (
            abs(net_flow_usd)
            <= VALUE_ZERO_TOLERANCE
        ):
            # Gift shares are treated as an in-kind
            # external contribution.
            return shares * price_usd

        return -net_flow_usd

    if side == "Sell":
        return -net_flow_usd

    raise ValueError(
        f"Unknown trade side: {side}"
    )


def validate_growth_factor(
    growth_factor: float,
    ticker: str,
    date: pd.Timestamp,
    calculation_name: str,
) -> float:
    """
    Validate a linked-return growth factor.
    """

    if not np.isfinite(growth_factor):
        raise ValueError(
            f"{ticker} produced a non-finite "
            f"{calculation_name} growth factor "
            f"on {date.date()}."
        )

    if growth_factor < -RETURN_VALIDATION_TOLERANCE:
        raise ValueError(
            f"{ticker} produced a negative "
            f"{calculation_name} growth factor "
            f"on {date.date()}: {growth_factor}"
        )

    if abs(growth_factor) <= RETURN_VALIDATION_TOLERANCE:
        return 0.0

    return float(growth_factor)


def get_daily_dividend_lookup(
    ticker_dividends: pd.DataFrame,
) -> dict[pd.Timestamp, float]:
    """
    Aggregate net dividend income by date.
    """

    if ticker_dividends.empty:
        return {}

    daily_dividends = (
        ticker_dividends
        .groupby("date")[
            "net_dividend_usd"
        ]
        .sum()
    )

    return {
        pd.Timestamp(date).normalize(): float(value)
        for date, value in daily_dividends.items()
    }


def get_daily_trade_lookup(
    ticker_trades: pd.DataFrame,
) -> dict[pd.Timestamp, pd.DataFrame]:
    """
    Group ordered trades by date.
    """

    lookup: dict[
        pd.Timestamp,
        pd.DataFrame,
    ] = {}

    for date, date_trades in (
        ticker_trades.groupby(
            "date",
            sort=True,
        )
    ):
        normalized_date = (
            pd.Timestamp(date).normalize()
        )

        lookup[normalized_date] = (
            date_trades
            .sort_values(
                by=[
                    "time",
                    "trade_id",
                ],
                kind="stable",
            )
            .reset_index(drop=True)
        )

    return lookup


def calculate_ticker_daily_history(
    ticker: str,
    ticker_trades: pd.DataFrame,
    ticker_dividends: pd.DataFrame,
    holding_row: pd.Series,
    calendar: pd.DatetimeIndex,
    daily_shares: pd.DataFrame,
    price_matrix: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Build daily valuation and transaction-event-based TWR
    for one security.
    """

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

    first_trade_date = (
        ticker_trades["date"].min()
    )

    end_date = pd.Timestamp(
        holding_row["as_of_date"]
    ).normalize()

    if end_date < first_trade_date:
        raise ValueError(
            f"{ticker} holding end date is before "
            "its first trade date."
        )

    ticker_calendar = calendar[
        (calendar >= first_trade_date)
        & (calendar <= end_date)
    ]

    if ticker_calendar.empty:
        raise RuntimeError(
            f"No security calendar dates were found "
            f"for {ticker}."
        )

    trade_lookup = get_daily_trade_lookup(
        ticker_trades
    )

    dividend_lookup = get_daily_dividend_lookup(
        ticker_dividends
    )

    current_shares = 0.0
    previous_close_price: float | None = None
    cumulative_growth_factor = 1.0

    rows: list[dict[str, Any]] = []

    unattributed_dividend_usd = 0.0
    linked_subperiod_count = 0

    for current_date in ticker_calendar:
        current_date = (
            pd.Timestamp(
                current_date
            ).normalize()
        )

        opening_shares = float(
            current_shares
        )

        if (
            opening_shares
            > VALUE_ZERO_TOLERANCE
            and previous_close_price is not None
        ):
            opening_value_usd = (
                opening_shares
                * previous_close_price
            )

        else:
            opening_value_usd = 0.0

        date_trades = trade_lookup.get(
            current_date
        )

        if date_trades is None:
            date_trades = pd.DataFrame(
                columns=ticker_trades.columns
            )

        dividend_usd = float(
            dividend_lookup.get(
                current_date,
                0.0,
            )
        )

        # Dividends are treated as the first event of the day.
        # A dividend can only be attributed to return when the
        # position existed at the beginning of the day.
        if (
            opening_shares
            > VALUE_ZERO_TOLERANCE
        ):
            pending_dividend_usd = (
                dividend_usd
            )

        else:
            pending_dividend_usd = 0.0

            if (
                dividend_usd
                > VALUE_ZERO_TOLERANCE
            ):
                unattributed_dividend_usd += (
                    dividend_usd
                )

        reference_price = (
            previous_close_price
            if (
                opening_shares
                > VALUE_ZERO_TOLERANCE
            )
            else None
        )

        daily_growth_factor = 1.0
        daily_subperiod_count = 0
        total_trade_flow_usd = 0.0

        for _, trade in (
            date_trades.iterrows()
        ):
            trade_price = float(
                trade["price_usd"]
            )

            trade_shares = float(
                trade["shares"]
            )

            side = str(
                trade["side"]
            )

            # Measure price movement from the previous
            # valuation point to the trade price.
            if (
                current_shares
                > VALUE_ZERO_TOLERANCE
                and reference_price is not None
            ):
                reference_value = (
                    current_shares
                    * reference_price
                )

                value_before_trade = (
                    current_shares
                    * trade_price
                )

                pre_trade_growth = (
                    (
                        value_before_trade
                        + pending_dividend_usd
                    )
                    / reference_value
                )

                pre_trade_growth = (
                    validate_growth_factor(
                        growth_factor=(
                            pre_trade_growth
                        ),
                        ticker=ticker,
                        date=current_date,
                        calculation_name=(
                            "pre-trade"
                        ),
                    )
                )

                daily_growth_factor *= (
                    pre_trade_growth
                )

                daily_subperiod_count += 1
                linked_subperiod_count += 1
                pending_dividend_usd = 0.0

            external_trade_flow = (
                calculate_external_trade_flow(
                    trade
                )
            )

            total_trade_flow_usd += (
                external_trade_flow
            )

            value_before_event = (
                current_shares
                * trade_price
            )

            if side == "Buy":
                shares_after_event = (
                    current_shares
                    + trade_shares
                )

            elif side == "Sell":
                shares_after_event = (
                    current_shares
                    - trade_shares
                )

            else:
                raise ValueError(
                    f"Unknown side for "
                    f"{ticker}: {side}"
                )

            if (
                shares_after_event
                < -SHARE_VALIDATION_TOLERANCE
            ):
                raise ValueError(
                    f"{ticker} shares became negative "
                    f"on {current_date.date()}."
                )

            if (
                abs(shares_after_event)
                <= VALUE_ZERO_TOLERANCE
            ):
                shares_after_event = 0.0

            value_after_event = (
                shares_after_event
                * trade_price
            )

            event_growth: float | None = None

            if (
                value_before_event
                > VALUE_ZERO_TOLERANCE
            ):
                event_growth = (
                    (
                        value_after_event
                        - external_trade_flow
                    )
                    / value_before_event
                )

            elif (
                external_trade_flow
                > VALUE_ZERO_TOLERANCE
            ):
                # This is normally the first purchase
                # or an in-kind gift contribution.
                event_growth = (
                    value_after_event
                    / external_trade_flow
                )

            if event_growth is not None:
                event_growth = (
                    validate_growth_factor(
                        growth_factor=(
                            event_growth
                        ),
                        ticker=ticker,
                        date=current_date,
                        calculation_name=(
                            "trade-event"
                        ),
                    )
                )

                daily_growth_factor *= (
                    event_growth
                )

                daily_subperiod_count += 1
                linked_subperiod_count += 1

            current_shares = float(
                shares_after_event
            )

            reference_price = (
                trade_price
                if (
                    current_shares
                    > VALUE_ZERO_TOLERANCE
                )
                else None
            )

        matrix_close_value = (
            price_matrix.at[
                current_date,
                ticker,
            ]
        )

        if pd.isna(matrix_close_value):
            if not date_trades.empty:
                close_usd = float(
                    date_trades[
                        "price_usd"
                    ].iloc[-1]
                )

            elif (
                current_shares
                > VALUE_ZERO_TOLERANCE
            ):
                raise RuntimeError(
                    f"{ticker} is missing a closing "
                    f"price on {current_date.date()}."
                )

            else:
                close_usd = 0.0

        else:
            close_usd = float(
                matrix_close_value
            )

        # Measure the final subperiod from the last trade
        # price, or previous close, to the current close.
        if (
            current_shares
            > VALUE_ZERO_TOLERANCE
        ):
            if reference_price is None:
                raise RuntimeError(
                    f"{ticker} has shares but no "
                    f"reference price on "
                    f"{current_date.date()}."
                )

            reference_value = (
                current_shares
                * reference_price
            )

            closing_value_before_flow = (
                current_shares
                * close_usd
            )

            closing_growth = (
                (
                    closing_value_before_flow
                    + pending_dividend_usd
                )
                / reference_value
            )

            closing_growth = (
                validate_growth_factor(
                    growth_factor=(
                        closing_growth
                    ),
                    ticker=ticker,
                    date=current_date,
                    calculation_name=(
                        "closing"
                    ),
                )
            )

            daily_growth_factor *= (
                closing_growth
            )

            daily_subperiod_count += 1
            linked_subperiod_count += 1
            pending_dividend_usd = 0.0

        if (
            daily_subperiod_count > 0
        ):
            daily_return = (
                daily_growth_factor - 1.0
            )

        else:
            daily_return = 0.0

        cumulative_growth_factor *= (
            1.0 + daily_return
        )

        if not np.isfinite(
            cumulative_growth_factor
        ):
            raise ValueError(
                f"{ticker} produced a non-finite "
                f"cumulative return on "
                f"{current_date.date()}."
            )

        closing_shares = float(
            current_shares
        )

        closing_value_usd = (
            closing_shares
            * close_usd
        )

        expected_closing_shares = float(
            daily_shares.at[
                current_date,
                ticker,
            ]
        )

        share_difference = (
            closing_shares
            - expected_closing_shares
        )

        if (
            abs(share_difference)
            > SHARE_VALIDATION_TOLERANCE
        ):
            raise RuntimeError(
                f"{ticker} share reconciliation "
                f"failed on {current_date.date()}. "
                f"Calculated: {closing_shares}, "
                f"expected: "
                f"{expected_closing_shares}"
            )

        date_text = (
            current_date.strftime(
                "%Y-%m-%d"
            )
        )

        rows.append(
            {
                "key": (
                    f"{ticker}|{date_text}"
                ),
                "ticker": ticker,
                "date": date_text,
                "opening_shares": (
                    opening_shares
                ),
                "closing_shares": (
                    closing_shares
                ),
                "close_usd": close_usd,
                "opening_value_usd": (
                    opening_value_usd
                ),
                "closing_value_usd": (
                    closing_value_usd
                ),
                "trade_flow_usd": (
                    total_trade_flow_usd
                ),
                "dividend_usd": (
                    dividend_usd
                ),
                "daily_return": float(
                    daily_return
                ),
                "cumulative_twr": float(
                    cumulative_growth_factor
                    - 1.0
                ),
            }
        )

        previous_close_price = (
            close_usd
            if (
                closing_shares
                > VALUE_ZERO_TOLERANCE
            )
            else None
        )

    ticker_daily = pd.DataFrame(rows)

    expected_final_shares = float(
        holding_row["shares"]
    )

    calculated_final_shares = float(
        ticker_daily[
            "closing_shares"
        ].iloc[-1]
    )

    final_share_difference = (
        calculated_final_shares
        - expected_final_shares
    )

    if (
        abs(final_share_difference)
        > SHARE_VALIDATION_TOLERANCE
    ):
        raise RuntimeError(
            f"{ticker} final share reconciliation "
            f"failed. Calculated: "
            f"{calculated_final_shares}, "
            f"holdings summary: "
            f"{expected_final_shares}"
        )

    metadata = {
        "ticker": ticker,
        "rows": len(ticker_daily),
        "status": holding_row["status"],
        "final_shares": (
            calculated_final_shares
        ),
        "final_cumulative_twr": float(
            ticker_daily[
                "cumulative_twr"
            ].iloc[-1]
        ),
        "linked_subperiod_count": (
            linked_subperiod_count
        ),
        "unattributed_dividend_usd": float(
            unattributed_dividend_usd
        ),
    }

    return ticker_daily, metadata


def build_security_daily() -> tuple[
    pd.DataFrame,
    dict[str, Any],
]:
    """
    Build daily valuation and TWR history
    for every traded security.
    """

    validate_database()

    trades = normalize_trade_order(
        load_trades()
    )

    dividends = load_dividends()
    holdings = load_holdings_summary()
    market_prices = load_market_prices()
    calendar = load_portfolio_calendar()

    traded_tickers = sorted(
        trades["ticker"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
        .tolist()
    )

    holdings_lookup = (
        holdings
        .set_index("ticker")
    )

    missing_holdings = [
        ticker
        for ticker in traded_tickers
        if ticker
        not in holdings_lookup.index
    ]

    if missing_holdings:
        raise RuntimeError(
            "The following tickers are missing "
            "from holdings_summary: "
            + ", ".join(missing_holdings)
        )

    daily_shares = (
        build_daily_share_matrix(
            trades=trades,
            calendar=calendar,
        )
    )

    price_matrix = (
        build_real_price_matrix(
            market_prices=market_prices,
            calendar=calendar,
            tickers=traded_tickers,
        )
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

    security_tables: list[
        pd.DataFrame
    ] = []

    ticker_metadata: list[
        dict[str, Any]
    ] = []

    for ticker in traded_tickers:
        ticker_trades = trades[
            trades["ticker"] == ticker
        ].copy()

        if dividends.empty:
            ticker_dividends = (
                dividends.copy()
            )

        else:
            ticker_dividends = dividends[
                dividends["ticker"]
                == ticker
            ].copy()

        holding_row = (
            holdings_lookup.loc[
                ticker
            ]
        )

        ticker_daily, metadata = (
            calculate_ticker_daily_history(
                ticker=ticker,
                ticker_trades=(
                    ticker_trades
                ),
                ticker_dividends=(
                    ticker_dividends
                ),
                holding_row=(
                    holding_row
                ),
                calendar=calendar,
                daily_shares=(
                    daily_shares
                ),
                price_matrix=(
                    price_matrix
                ),
            )
        )

        security_tables.append(
            ticker_daily
        )

        ticker_metadata.append(
            metadata
        )

    if not security_tables:
        raise RuntimeError(
            "No security daily records "
            "were created."
        )

    security_daily = pd.concat(
        security_tables,
        ignore_index=True,
    )

    security_daily = (
        security_daily
        .sort_values(
            by=[
                "ticker",
                "date",
            ],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    duplicate_keys = (
        security_daily["key"]
        .duplicated(keep=False)
    )

    if duplicate_keys.any():
        duplicates = sorted(
            security_daily.loc[
                duplicate_keys,
                "key",
            ]
            .astype(str)
            .unique()
            .tolist()
        )

        raise RuntimeError(
            "Duplicate security_daily keys "
            "were produced: "
            + ", ".join(
                duplicates[:10]
            )
        )

    metadata_table = pd.DataFrame(
        ticker_metadata
    )

    total_unattributed_dividends = float(
        metadata_table[
            "unattributed_dividend_usd"
        ].sum()
    )

    metadata = {
        "saved_ticker_count": (
            len(traded_tickers)
        ),
        "row_count": (
            len(security_daily)
        ),
        "fallback_tickers": (
            fallback_tickers
        ),
        "ticker_results": (
            metadata_table
        ),
        "total_unattributed_dividend_usd": (
            total_unattributed_dividends
        ),
    }

    return security_daily, metadata


def save_security_daily(
    security_daily: pd.DataFrame,
) -> int:
    """
    Replace the stored security_daily table.
    """

    required_columns = [
        "key",
        "ticker",
        "date",
        "opening_shares",
        "closing_shares",
        "close_usd",
        "opening_value_usd",
        "closing_value_usd",
        "trade_flow_usd",
        "dividend_usd",
        "daily_return",
        "cumulative_twr",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column
        not in security_daily.columns
    ]

    if missing_columns:
        raise ValueError(
            "security_daily is missing columns: "
            + ", ".join(
                missing_columns
            )
        )

    rows = [
        (
            str(row.key),
            str(row.ticker),
            str(row.date),
            float(
                row.opening_shares
            ),
            float(
                row.closing_shares
            ),
            float(
                row.close_usd
            ),
            float(
                row.opening_value_usd
            ),
            float(
                row.closing_value_usd
            ),
            float(
                row.trade_flow_usd
            ),
            float(
                row.dividend_usd
            ),
            float(
                row.daily_return
            ),
            float(
                row.cumulative_twr
            ),
        )
        for row in security_daily[
            required_columns
        ].itertuples(index=False)
    ]

    query = """
        INSERT INTO security_daily (
            key,
            ticker,
            date,
            opening_shares,
            closing_shares,
            close_usd,
            opening_value_usd,
            closing_value_usd,
            trade_flow_usd,
            dividend_usd,
            daily_return,
            cumulative_twr
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    with database_transaction() as connection:
        connection.execute(
            "DELETE FROM security_daily"
        )

        if rows:
            connection.executemany(
                query,
                rows,
            )

    return len(rows)


def refresh_security_daily() -> dict[str, Any]:
    """
    Calculate, validate and save security daily data.
    """

    security_daily, metadata = (
        build_security_daily()
    )

    saved_rows = save_security_daily(
        security_daily
    )

    return {
        "saved_rows": saved_rows,
        **metadata,
    }


def main() -> None:
    """
    Refresh security_daily and print validation results.
    """

    result = refresh_security_daily()

    print(
        "Security daily table "
        "refreshed successfully."
    )

    print(
        f"Rows saved: "
        f"{result['saved_rows']}"
    )

    print(
        f"Tickers processed: "
        f"{result['saved_ticker_count']}"
    )

    fallback_tickers = result[
        "fallback_tickers"
    ]

    print(
        "Trade-price fallback tickers: "
        + (
            ", ".join(
                fallback_tickers
            )
            if fallback_tickers
            else "None"
        )
    )

    print(
        "Unattributed dividends: "
        f"${result['total_unattributed_dividend_usd']}"
    )

    ticker_results = result[
        "ticker_results"
    ]

    with pd.option_context(
        "display.max_rows",
        None,
        "display.max_columns",
        None,
        "display.width",
        180,
        "display.float_format",
        lambda value: f"{value:.10f}",
    ):
        print()
        print("SECURITY DAILY VALIDATION")
        print("-" * 80)

        print(
            ticker_results.to_string(
                index=False
            )
        )


if __name__ == "__main__":
    main()
