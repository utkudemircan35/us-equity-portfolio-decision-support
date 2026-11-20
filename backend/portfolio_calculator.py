from dataclasses import dataclass
from typing import Any

from database import (
    database_transaction,
    fetch_all,
    fetch_one,
    validate_database,
)


SHARE_ZERO_TOLERANCE = 1e-10
SHARE_VALIDATION_TOLERANCE = 1e-8


@dataclass
class PositionState:
    """
    Store the running cost-basis state of one security.
    """

    ticker: str
    shares: float = 0.0
    remaining_cost_basis_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    last_trade_date: str | None = None


def load_trades() -> list[dict[str, Any]]:
    """
    Load all trades in chronological order.
    """

    return fetch_all(
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


def calculate_position_states() -> dict[str, PositionState]:
    """
    Calculate shares, remaining cost basis and realized P&L.
    """

    trades = load_trades()

    if not trades:
        raise RuntimeError(
            "No trades were found in the database."
        )

    positions: dict[str, PositionState] = {}

    for trade in trades:
        ticker = str(trade["ticker"]).strip().upper()
        side = str(trade["side"]).strip().title()

        trade_shares = float(trade["shares"])
        net_flow_usd = float(trade["net_flow_usd"])

        if ticker not in positions:
            positions[ticker] = PositionState(
                ticker=ticker
            )

        position = positions[ticker]
        position.last_trade_date = trade["date"]

        if side == "Buy":
            # Normal purchases have a negative net flow.
            # Gift shares have zero net flow and zero cost basis.
            purchase_cost_usd = -net_flow_usd

            if purchase_cost_usd < 0:
                raise ValueError(
                    f"Invalid buy net flow for {ticker} "
                    f"at trade ID {trade['trade_id']}."
                )

            position.shares += trade_shares

            position.remaining_cost_basis_usd += (
                purchase_cost_usd
            )

        elif side == "Sell":
            if position.shares <= SHARE_ZERO_TOLERANCE:
                raise ValueError(
                    f"Cannot sell {ticker} because no shares "
                    f"are available before trade ID "
                    f"{trade['trade_id']}."
                )

            if (
                trade_shares - position.shares
                > SHARE_VALIDATION_TOLERANCE
            ):
                raise ValueError(
                    f"Trade ID {trade['trade_id']} sells "
                    f"{trade_shares} shares of {ticker}, "
                    f"but only {position.shares} shares "
                    f"are available."
                )

            average_cost_before_sale = (
                position.remaining_cost_basis_usd
                / position.shares
            )

            sold_cost_basis_usd = (
                average_cost_before_sale
                * trade_shares
            )

            position.realized_pnl_usd += (
                net_flow_usd
                - sold_cost_basis_usd
            )

            position.shares -= trade_shares

            position.remaining_cost_basis_usd -= (
                sold_cost_basis_usd
            )

            # Remove insignificant floating-point residues.
            if (
                abs(position.shares)
                <= SHARE_ZERO_TOLERANCE
            ):
                position.shares = 0.0
                position.remaining_cost_basis_usd = 0.0

        else:
            raise ValueError(
                f"Unknown trade side '{side}' "
                f"at trade ID {trade['trade_id']}."
            )

        expected_cumulative_shares = float(
            trade["cumulative_shares"]
        )

        if (
            abs(
                position.shares
                - expected_cumulative_shares
            )
            > SHARE_VALIDATION_TOLERANCE
        ):
            raise ValueError(
                f"Cumulative shares mismatch for {ticker} "
                f"at trade ID {trade['trade_id']}. "
                f"Calculated: {position.shares}, "
                f"stored: {expected_cumulative_shares}."
            )

    return positions


def get_open_tickers(
    positions: dict[str, PositionState],
) -> list[str]:
    """
    Return tickers with a non-zero current share balance.
    """

    return sorted(
        ticker
        for ticker, position in positions.items()
        if abs(position.shares)
        > SHARE_ZERO_TOLERANCE
    )


def load_current_prices(
    open_tickers: list[str],
) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """
    Load closing prices using a common portfolio valuation date.
    """

    if not open_tickers:
        return None, {}

    placeholders = ", ".join(
        "?"
        for _ in open_tickers
    )

    latest_dates = fetch_all(
        f"""
        SELECT
            ticker,
            MAX(date) AS latest_date
        FROM market_prices
        WHERE ticker IN ({placeholders})
        GROUP BY ticker
        """,
        tuple(open_tickers),
    )

    ticker_latest_dates = {
        row["ticker"]: row["latest_date"]
        for row in latest_dates
    }

    missing_tickers = [
        ticker
        for ticker in open_tickers
        if ticker not in ticker_latest_dates
        or ticker_latest_dates[ticker] is None
    ]

    if missing_tickers:
        raise RuntimeError(
            "Current prices are missing for open positions: "
            + ", ".join(missing_tickers)
        )

    # Use the latest date that is available for every open position.
    valuation_date = min(
        ticker_latest_dates.values()
    )

    price_rows = fetch_all(
        f"""
        WITH ranked_prices AS (
            SELECT
                ticker,
                date,
                close_usd,
                adjusted_close_usd,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY date DESC
                ) AS row_number
            FROM market_prices
            WHERE ticker IN ({placeholders})
              AND date <= ?
        )
        SELECT
            ticker,
            date,
            close_usd,
            adjusted_close_usd
        FROM ranked_prices
        WHERE row_number = 1
        ORDER BY ticker
        """,
        tuple(open_tickers) + (valuation_date,),
    )

    prices = {
        row["ticker"]: {
            "date": row["date"],
            "close_usd": float(
                row["close_usd"]
            ),
            "adjusted_close_usd": float(
                row["adjusted_close_usd"]
            ),
        }
        for row in price_rows
    }

    missing_tickers = [
        ticker
        for ticker in open_tickers
        if ticker not in prices
    ]

    if missing_tickers:
        raise RuntimeError(
            "Prices on or before the valuation date "
            "are missing for: "
            + ", ".join(missing_tickers)
        )

    return valuation_date, prices


def calculate_cash_balance() -> dict[str, float]:
    """
    Calculate the current USD cash balance.
    """

    cash_movement_result = fetch_one(
        """
        SELECT
            COALESCE(
                SUM(
                    CASE
                        WHEN type = 'DEPOSIT'
                            THEN amount_usd
                        WHEN type = 'INTEREST'
                            THEN amount_usd
                        WHEN type = 'WITHDRAWAL'
                            THEN -amount_usd
                        ELSE 0
                    END
                ),
                0
            ) AS cash_movement_total
        FROM cash_movements
        """
    )

    trade_result = fetch_one(
        """
        SELECT
            COALESCE(
                SUM(net_flow_usd),
                0
            ) AS trade_cash_flow
        FROM trades
        """
    )

    dividend_result = fetch_one(
        """
        SELECT
            COALESCE(
                SUM(net_dividend_usd),
                0
            ) AS dividend_cash_flow
        FROM dividends
        """
    )

    cash_movement_total = float(
        cash_movement_result["cash_movement_total"]
    )

    trade_cash_flow = float(
        trade_result["trade_cash_flow"]
    )

    dividend_cash_flow = float(
        dividend_result["dividend_cash_flow"]
    )

    cash_balance_usd = (
        cash_movement_total
        + trade_cash_flow
        + dividend_cash_flow
    )

    return {
        "cash_movement_total": cash_movement_total,
        "trade_cash_flow": trade_cash_flow,
        "dividend_cash_flow": dividend_cash_flow,
        "cash_balance_usd": cash_balance_usd,
    }


def get_latest_event_date() -> str:
    """
    Return the latest date found in any transaction table.
    """

    result = fetch_one(
        """
        SELECT MAX(event_date) AS latest_event_date
        FROM (
            SELECT date AS event_date
            FROM trades

            UNION ALL

            SELECT date AS event_date
            FROM cash_movements

            UNION ALL

            SELECT date AS event_date
            FROM dividends
        )
        """
    )

    if (
        result is None
        or result["latest_event_date"] is None
    ):
        raise RuntimeError(
            "No portfolio event date was found."
        )

    return result["latest_event_date"]


def build_holdings_summary() -> tuple[
    list[dict[str, Any]],
    dict[str, float],
]:
    """
    Build security and cash rows for holdings_summary.
    """

    positions = calculate_position_states()
    open_tickers = get_open_tickers(positions)

    valuation_date, current_prices = (
        load_current_prices(open_tickers)
    )

    cash_details = calculate_cash_balance()

    cash_balance_usd = cash_details[
        "cash_balance_usd"
    ]

    security_rows: list[dict[str, Any]] = []
    total_securities_value_usd = 0.0

    for ticker in sorted(positions):
        position = positions[ticker]

        is_open = (
            abs(position.shares)
            > SHARE_ZERO_TOLERANCE
        )

        if is_open:
            price_information = current_prices[ticker]

            price_usd = price_information[
                "close_usd"
            ]

            row_as_of_date = price_information[
                "date"
            ]

            market_value_usd = (
                position.shares
                * price_usd
            )

            average_cost_usd = (
                position.remaining_cost_basis_usd
                / position.shares
            )

            unrealized_pnl_usd = (
                market_value_usd
                - position.remaining_cost_basis_usd
            )

            status = "OPEN"

            total_securities_value_usd += (
                market_value_usd
            )

        else:
            price_usd = 0.0
            market_value_usd = 0.0
            average_cost_usd = 0.0
            unrealized_pnl_usd = 0.0
            status = "CLOSED"

            row_as_of_date = (
                position.last_trade_date
            )

        security_rows.append(
            {
                "ticker": ticker,
                "as_of_date": row_as_of_date,
                "status": status,
                "shares": position.shares,
                "price_usd": price_usd,
                "market_value_usd": (
                    market_value_usd
                ),
                "weight": 0.0,
                "remaining_cost_basis_usd": (
                    position.remaining_cost_basis_usd
                ),
                "average_cost_usd": (
                    average_cost_usd
                ),
                "realized_pnl_usd": (
                    position.realized_pnl_usd
                ),
                "unrealized_pnl_usd": (
                    unrealized_pnl_usd
                ),
            }
        )

    total_portfolio_value_usd = (
        total_securities_value_usd
        + cash_balance_usd
    )

    if (
        abs(total_portfolio_value_usd)
        <= SHARE_ZERO_TOLERANCE
    ):
        raise RuntimeError(
            "Total portfolio value is zero. "
            "Portfolio weights cannot be calculated."
        )

    for row in security_rows:
        row["weight"] = (
            row["market_value_usd"]
            / total_portfolio_value_usd
        )

    if valuation_date is None:
        cash_as_of_date = get_latest_event_date()

    else:
        cash_as_of_date = max(
            valuation_date,
            get_latest_event_date(),
        )

    cash_row = {
        "ticker": "CASH",
        "as_of_date": cash_as_of_date,
        "status": "OPEN",
        "shares": cash_balance_usd,
        "price_usd": 1.0,
        "market_value_usd": cash_balance_usd,
        "weight": (
            cash_balance_usd
            / total_portfolio_value_usd
        ),
        "remaining_cost_basis_usd": (
            cash_balance_usd
        ),
        "average_cost_usd": 1.0,
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
    }

    summary_rows = security_rows + [cash_row]

    totals = {
        **cash_details,
        "total_securities_value_usd": (
            total_securities_value_usd
        ),
        "total_portfolio_value_usd": (
            total_portfolio_value_usd
        ),
    }

    return summary_rows, totals


def save_holdings_summary(
    rows: list[dict[str, Any]],
) -> int:
    """
    Replace the holdings_summary table with calculated rows.
    """

    query = """
        INSERT INTO holdings_summary (
            ticker,
            as_of_date,
            status,
            shares,
            price_usd,
            market_value_usd,
            weight,
            remaining_cost_basis_usd,
            average_cost_usd,
            realized_pnl_usd,
            unrealized_pnl_usd
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    values = [
        (
            row["ticker"],
            row["as_of_date"],
            row["status"],
            float(row["shares"]),
            float(row["price_usd"]),
            float(row["market_value_usd"]),
            float(row["weight"]),
            float(
                row["remaining_cost_basis_usd"]
            ),
            float(row["average_cost_usd"]),
            float(row["realized_pnl_usd"]),
            float(row["unrealized_pnl_usd"]),
        )
        for row in rows
    ]

    with database_transaction() as connection:
        connection.execute(
            "DELETE FROM holdings_summary"
        )

        connection.executemany(
            query,
            values,
        )

    return len(values)


def refresh_holdings_summary() -> dict[str, Any]:
    """
    Calculate and save the current portfolio holdings.
    """

    validate_database()

    rows, totals = build_holdings_summary()

    saved_rows = save_holdings_summary(rows)

    open_positions = sum(
        1
        for row in rows
        if (
            row["ticker"] != "CASH"
            and row["status"] == "OPEN"
        )
    )

    closed_positions = sum(
        1
        for row in rows
        if (
            row["ticker"] != "CASH"
            and row["status"] == "CLOSED"
        )
    )

    return {
        "saved_rows": saved_rows,
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        **totals,
    }


def main() -> None:
    """
    Refresh holdings_summary and print calculation results.
    """

    result = refresh_holdings_summary()

    print("Holdings summary refreshed successfully.")
    print(
        f"Rows saved: {result['saved_rows']}"
    )
    print(
        f"Open positions: "
        f"{result['open_positions']}"
    )
    print(
        f"Closed positions: "
        f"{result['closed_positions']}"
    )
    print(
        f"Cash balance: "
        f"{result['cash_balance_usd']}"
    )
    print(
        f"Securities value: "
        f"{result['total_securities_value_usd']}"
    )
    print(
        f"Portfolio value: "
        f"{result['total_portfolio_value_usd']}"
    )


if __name__ == "__main__":
    main()
