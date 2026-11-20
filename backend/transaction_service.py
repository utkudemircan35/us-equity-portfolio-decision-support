from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable

import pandas as pd
import yfinance as yf

import database
from database import (
    database_transaction,
    fetch_all,
    fetch_one,
    validate_database,
)
from excel_importer import (
    normalize_boolean_value,
    normalize_time_value,
)
from portfolio_pipeline import (
    ProgressCallback,
    console_progress_callback,
)
from portfolio_refresh_service import (
    PortfolioRefreshResult,
    run_portfolio_refresh,
)


SHARE_ZERO_TOLERANCE = 1e-10
SHARE_VALIDATION_TOLERANCE = 1e-8


@dataclass
class MutationResult:
    table_name: str
    operation: str
    record_id: int
    refresh_result: (
        PortfolioRefreshResult | None
    )


def rows_to_dataframe(
    rows: list[dict[str, Any]],
) -> pd.DataFrame:
    """
    Convert database rows to a DataFrame.
    """

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


def normalize_date_value(
    value: Any,
) -> str:
    """
    Convert a date value to YYYY-MM-DD.
    """

    parsed_value = pd.to_datetime(
        value,
        errors="coerce",
    )

    if pd.isna(parsed_value):
        raise ValueError(
            f"Invalid date value: {value}"
        )

    return parsed_value.strftime(
        "%Y-%m-%d"
    )


def normalize_ticker(
    value: Any,
) -> str:
    """
    Normalize a ticker value.
    """

    ticker = (
        str(value)
        .strip()
        .upper()
    )

    if not ticker:
        raise ValueError(
            "Ticker cannot be empty."
        )

    return ticker


def normalize_side(
    value: Any,
) -> str:
    """
    Normalize a trade side.
    """

    side = (
        str(value)
        .strip()
        .title()
    )

    if side not in {
        "Buy",
        "Sell",
    }:
        raise ValueError(
            "Trade side must be "
            "Buy or Sell."
        )

    return side


def normalize_positive_number(
    value: Any,
    field_name: str,
) -> float:
    """
    Convert a value to a positive float.
    """

    try:
        number = float(value)

    except (
        TypeError,
        ValueError,
    ) as error:
        raise ValueError(
            f"{field_name} must be numeric."
        ) from error

    if not pd.notna(number):
        raise ValueError(
            f"{field_name} must be numeric."
        )

    if number <= 0:
        raise ValueError(
            f"{field_name} must be "
            "greater than zero."
        )

    return number


def normalize_non_negative_number(
    value: Any,
    field_name: str,
) -> float:
    """
    Convert a value to a non-negative float.
    """

    try:
        number = float(value)

    except (
        TypeError,
        ValueError,
    ) as error:
        raise ValueError(
            f"{field_name} must be numeric."
        ) from error

    if not pd.notna(number):
        raise ValueError(
            f"{field_name} must be numeric."
        )

    if number < 0:
        raise ValueError(
            f"{field_name} cannot "
            "be negative."
        )

    return number


def download_split_history_strict(
    ticker: str,
) -> list[
    tuple[pd.Timestamp, float]
]:
    """
    Download split history for one ticker.

    Network errors are raised instead of silently
    assuming a split factor of one.
    """

    print(
        f"Checking stock splits: {ticker}"
    )

    try:
        splits = (
            yf.Ticker(ticker)
            .get_splits(
                period="max"
            )
        )

    except Exception as error:
        raise RuntimeError(
            "Stock split history could not "
            f"be downloaded for {ticker}: "
            f"{error}"
        ) from error

    if splits is None:
        raise RuntimeError(
            "Yahoo Finance returned an invalid "
            f"split response for {ticker}."
        )

    if splits.empty:
        return []

    result: list[
        tuple[pd.Timestamp, float]
    ] = []

    for (
        split_datetime,
        split_ratio,
    ) in splits.items():
        split_date = pd.Timestamp(
            split_datetime
        )

        if split_date.tzinfo is not None:
            split_date = (
                split_date.tz_localize(
                    None
                )
            )

        split_date = (
            split_date.normalize()
        )

        ratio = float(
            split_ratio
        )

        if ratio <= 0:
            raise ValueError(
                f"{ticker} returned an invalid "
                f"split ratio: {ratio}"
            )

        if ratio == 1.0:
            continue

        result.append(
            (
                split_date,
                ratio,
            )
        )

    result.sort(
        key=lambda item: item[0]
    )

    return result


def calculate_split_factor(
    trade_date: str,
    split_history: list[
        tuple[pd.Timestamp, float]
    ],
) -> float:
    """
    Calculate the cumulative split factor
    after a trade date.
    """

    normalized_trade_date = (
        pd.Timestamp(
            trade_date
        ).normalize()
    )

    factor = 1.0

    for (
        split_date,
        split_ratio,
    ) in split_history:
        # Trades on the effective split date are
        # already expressed in post-split units.
        if split_date > normalized_trade_date:
            factor *= split_ratio

    return float(factor)


def prepare_trade_values(
    date: Any,
    time_value: Any,
    ticker: Any,
    side: Any,
    raw_shares: Any,
    raw_price_usd: Any,
    commission_usd: Any = 0.0,
    is_gift: Any = False,
) -> dict[str, Any]:
    """
    Validate and calculate one trade record.
    """

    normalized_date = (
        normalize_date_value(date)
    )

    normalized_time = (
        normalize_time_value(
            time_value,
            allow_missing=True,
        )
    )

    normalized_ticker = (
        normalize_ticker(ticker)
    )

    normalized_side = (
        normalize_side(side)
    )

    normalized_raw_shares = (
        normalize_positive_number(
            raw_shares,
            "raw_shares",
        )
    )

    normalized_raw_price = (
        normalize_positive_number(
            raw_price_usd,
            "raw_price_usd",
        )
    )

    normalized_commission = (
        normalize_non_negative_number(
            commission_usd,
            "commission_usd",
        )
    )

    normalized_is_gift = (
        normalize_boolean_value(
            is_gift
        )
    )

    if (
        normalized_is_gift
        and normalized_side != "Buy"
    ):
        raise ValueError(
            "A gift transaction must "
            "use Buy as its side."
        )

    if (
        normalized_is_gift
        and normalized_commission
        > SHARE_ZERO_TOLERANCE
    ):
        raise ValueError(
            "A gift transaction cannot "
            "have a commission."
        )

    split_history = (
        download_split_history_strict(
            normalized_ticker
        )
    )

    split_factor = (
        calculate_split_factor(
            trade_date=normalized_date,
            split_history=split_history,
        )
    )

    shares = (
        normalized_raw_shares
        * split_factor
    )

    price_usd = (
        normalized_raw_price
        / split_factor
    )

    gross_value = (
        shares
        * price_usd
    )

    if normalized_is_gift:
        net_flow_usd = 0.0

    elif normalized_side == "Buy":
        net_flow_usd = -(
            gross_value
            + normalized_commission
        )

    else:
        net_flow_usd = (
            gross_value
            - normalized_commission
        )

        if net_flow_usd < 0:
            raise ValueError(
                "Sell commission cannot exceed "
                "the gross sale value."
            )

    return {
        "date": normalized_date,
        "time": normalized_time,
        "ticker": normalized_ticker,
        "side": normalized_side,
        "raw_shares": (
            normalized_raw_shares
        ),
        "raw_price_usd": (
            normalized_raw_price
        ),
        "split_factor": (
            split_factor
        ),
        "is_gift": int(
            normalized_is_gift
        ),
        "shares": float(shares),
        "price_usd": float(
            price_usd
        ),
        "commission_usd": float(
            normalized_commission
        ),
        "net_flow_usd": float(
            net_flow_usd
        ),
    }


def recalculate_trade_sequence(
    connection,
) -> None:
    """
    Recalculate net flow and cumulative shares
    for every trade in chronological order.
    """

    rows = connection.execute(
        """
        SELECT
            trade_id,
            date,
            time,
            ticker,
            side,
            is_gift,
            shares,
            price_usd,
            commission_usd
        FROM trades
        ORDER BY
            date,
            COALESCE(
                time,
                '00:00:00'
            ),
            trade_id
        """
    ).fetchall()

    balances: dict[str, float] = {}

    updates: list[
        tuple[float, float, int]
    ] = []

    for row in rows:
        trade_id = int(
            row["trade_id"]
        )

        ticker = str(
            row["ticker"]
        )

        side = str(
            row["side"]
        )

        shares = float(
            row["shares"]
        )

        price_usd = float(
            row["price_usd"]
        )

        commission_usd = float(
            row["commission_usd"]
        )

        is_gift = bool(
            row["is_gift"]
        )

        gross_value = (
            shares
            * price_usd
        )

        if is_gift:
            if side != "Buy":
                raise ValueError(
                    f"Trade ID {trade_id} is a "
                    "gift but is not a Buy."
                )

            net_flow_usd = 0.0

        elif side == "Buy":
            net_flow_usd = -(
                gross_value
                + commission_usd
            )

        elif side == "Sell":
            net_flow_usd = (
                gross_value
                - commission_usd
            )

            if net_flow_usd < 0:
                raise ValueError(
                    f"Trade ID {trade_id} has a "
                    "commission greater than its "
                    "gross sale value."
                )

        else:
            raise ValueError(
                f"Trade ID {trade_id} has "
                f"an unknown side: {side}"
            )

        previous_balance = float(
            balances.get(
                ticker,
                0.0,
            )
        )

        if side == "Buy":
            new_balance = (
                previous_balance
                + shares
            )

        else:
            new_balance = (
                previous_balance
                - shares
            )

        if (
            new_balance
            < -SHARE_VALIDATION_TOLERANCE
        ):
            raise ValueError(
                f"Trade ID {trade_id} sells "
                f"{shares} shares of {ticker}, "
                f"but only {previous_balance} "
                "shares are available."
            )

        if (
            abs(new_balance)
            <= SHARE_ZERO_TOLERANCE
        ):
            new_balance = 0.0

        balances[ticker] = (
            new_balance
        )

        updates.append(
            (
                float(net_flow_usd),
                float(new_balance),
                trade_id,
            )
        )

    connection.executemany(
        """
        UPDATE trades
        SET
            net_flow_usd = ?,
            cumulative_shares = ?
        WHERE trade_id = ?
        """,
        updates,
    )


def create_database_snapshot() -> Path:
    """
    Create a consistent temporary SQLite snapshot.
    """

    file_descriptor, snapshot_name = (
        tempfile.mkstemp(
            prefix="portfolio_snapshot_",
            suffix=".db",
        )
    )

    os.close(file_descriptor)

    snapshot_path = Path(
        snapshot_name
    )

    source_connection = (
        database.get_connection()
    )

    snapshot_connection = (
        sqlite3.connect(
            snapshot_path
        )
    )

    try:
        source_connection.backup(
            snapshot_connection
        )

        snapshot_connection.commit()

    finally:
        snapshot_connection.close()
        source_connection.close()

    return snapshot_path


def restore_database_snapshot(
    snapshot_path: Path,
) -> None:
    """
    Restore the main database from a snapshot.
    """

    if not snapshot_path.exists():
        raise FileNotFoundError(
            f"Snapshot does not exist: "
            f"{snapshot_path}"
        )

    snapshot_connection = (
        sqlite3.connect(
            snapshot_path
        )
    )

    target_connection = (
        database.get_connection()
    )

    try:
        snapshot_connection.backup(
            target_connection
        )

        target_connection.commit()

    finally:
        target_connection.close()
        snapshot_connection.close()

    validate_database()


def run_safe_mutation(
    table_name: str,
    operation: str,
    mutation: Callable[[], int],
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Run a database mutation with automatic snapshot rollback.
    """

    validate_database()

    snapshot_path = (
        create_database_snapshot()
    )

    record_id: int | None = None

    try:
        record_id = int(
            mutation()
        )

        if refresh_calculated_data:
            refresh_result = (
                run_portfolio_refresh(
                    progress_callback=(
                        progress_callback
                    )
                )
            )

        else:
            refresh_result = None

            print(
                "Warning: calculated portfolio "
                "tables were not refreshed."
            )

        return MutationResult(
            table_name=table_name,
            operation=operation,
            record_id=record_id,
            refresh_result=(
                refresh_result
            ),
        )

    except Exception as error:
        restore_database_snapshot(
            snapshot_path
        )

        raise RuntimeError(
            f"{operation} failed for "
            f"{table_name}. The database "
            "was restored to its previous "
            f"state. Original error: {error}"
        ) from error

    finally:
        snapshot_path.unlink(
            missing_ok=True
        )


def list_trades() -> pd.DataFrame:
    """
    Return all trades in chronological order.
    """

    rows = fetch_all(
        """
        SELECT
            trade_id,
            date,
            time,
            ticker,
            side,
            raw_shares,
            raw_price_usd,
            split_factor,
            is_gift,
            shares,
            price_usd,
            commission_usd,
            net_flow_usd,
            cumulative_shares
        FROM trades
        ORDER BY
            date,
            COALESCE(
                time,
                '00:00:00'
            ),
            trade_id
        """
    )

    return rows_to_dataframe(rows)


def get_trade(
    trade_id: int,
) -> dict[str, Any]:
    """
    Return one trade.
    """

    row = fetch_one(
        """
        SELECT
            trade_id,
            date,
            time,
            ticker,
            side,
            raw_shares,
            raw_price_usd,
            split_factor,
            is_gift,
            shares,
            price_usd,
            commission_usd,
            net_flow_usd,
            cumulative_shares
        FROM trades
        WHERE trade_id = ?
        """,
        (int(trade_id),),
    )

    if row is None:
        raise ValueError(
            f"Trade ID {trade_id} "
            "does not exist."
        )

    return row


def add_trade(
    date: Any,
    ticker: Any,
    side: Any,
    raw_shares: Any,
    raw_price_usd: Any,
    commission_usd: Any = 0.0,
    time_value: Any = None,
    is_gift: Any = False,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Add one trade and rebuild calculated data.
    """

    values = prepare_trade_values(
        date=date,
        time_value=time_value,
        ticker=ticker,
        side=side,
        raw_shares=raw_shares,
        raw_price_usd=raw_price_usd,
        commission_usd=(
            commission_usd
        ),
        is_gift=is_gift,
    )

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trades (
                    date,
                    time,
                    ticker,
                    side,
                    raw_shares,
                    raw_price_usd,
                    split_factor,
                    is_gift,
                    shares,
                    price_usd,
                    commission_usd,
                    net_flow_usd,
                    cumulative_shares
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    values["date"],
                    values["time"],
                    values["ticker"],
                    values["side"],
                    values["raw_shares"],
                    values[
                        "raw_price_usd"
                    ],
                    values["split_factor"],
                    values["is_gift"],
                    values["shares"],
                    values["price_usd"],
                    values[
                        "commission_usd"
                    ],
                    values["net_flow_usd"],
                    0.0,
                ),
            )

            record_id = int(
                cursor.lastrowid
            )

            recalculate_trade_sequence(
                connection
            )

            return record_id

    return run_safe_mutation(
        table_name="trades",
        operation="ADD",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def edit_trade(
    trade_id: int,
    date: Any,
    ticker: Any,
    side: Any,
    raw_shares: Any,
    raw_price_usd: Any,
    commission_usd: Any = 0.0,
    time_value: Any = None,
    is_gift: Any = False,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Replace all editable fields of one trade.
    """

    get_trade(trade_id)

    values = prepare_trade_values(
        date=date,
        time_value=time_value,
        ticker=ticker,
        side=side,
        raw_shares=raw_shares,
        raw_price_usd=raw_price_usd,
        commission_usd=(
            commission_usd
        ),
        is_gift=is_gift,
    )

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE trades
                SET
                    date = ?,
                    time = ?,
                    ticker = ?,
                    side = ?,
                    raw_shares = ?,
                    raw_price_usd = ?,
                    split_factor = ?,
                    is_gift = ?,
                    shares = ?,
                    price_usd = ?,
                    commission_usd = ?,
                    net_flow_usd = ?
                WHERE trade_id = ?
                """,
                (
                    values["date"],
                    values["time"],
                    values["ticker"],
                    values["side"],
                    values["raw_shares"],
                    values[
                        "raw_price_usd"
                    ],
                    values["split_factor"],
                    values["is_gift"],
                    values["shares"],
                    values["price_usd"],
                    values[
                        "commission_usd"
                    ],
                    values["net_flow_usd"],
                    int(trade_id),
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Trade ID {trade_id} "
                    "could not be updated."
                )

            recalculate_trade_sequence(
                connection
            )

            return int(trade_id)

    return run_safe_mutation(
        table_name="trades",
        operation="EDIT",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def delete_trade(
    trade_id: int,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Delete one trade and validate the remaining history.
    """

    get_trade(trade_id)

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM trades
                WHERE trade_id = ?
                """,
                (int(trade_id),),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Trade ID {trade_id} "
                    "could not be deleted."
                )

            recalculate_trade_sequence(
                connection
            )

            return int(trade_id)

    return run_safe_mutation(
        table_name="trades",
        operation="DELETE",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def normalize_cash_movement_type(
    value: Any,
) -> str:
    """
    Normalize a cash-movement type.
    """

    movement_type = (
        str(value)
        .strip()
        .upper()
    )

    allowed_types = {
        "DEPOSIT",
        "WITHDRAWAL",
        "INTEREST",
    }

    if movement_type not in allowed_types:
        raise ValueError(
            "Cash movement type must be "
            "DEPOSIT, WITHDRAWAL or INTEREST."
        )

    return movement_type


def list_cash_movements() -> pd.DataFrame:
    """
    Return all cash movements.
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
            COALESCE(
                time,
                '00:00:00'
            ),
            cash_id
        """
    )

    return rows_to_dataframe(rows)


def get_cash_movement(
    cash_id: int,
) -> dict[str, Any]:
    """
    Return one cash movement.
    """

    row = fetch_one(
        """
        SELECT
            cash_id,
            date,
            time,
            type,
            amount_usd
        FROM cash_movements
        WHERE cash_id = ?
        """,
        (int(cash_id),),
    )

    if row is None:
        raise ValueError(
            f"Cash movement ID {cash_id} "
            "does not exist."
        )

    return row


def add_cash_movement(
    date: Any,
    movement_type: Any,
    amount_usd: Any,
    time_value: Any = None,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Add one cash movement.
    """

    normalized_date = (
        normalize_date_value(date)
    )

    normalized_time = (
        normalize_time_value(
            time_value,
            allow_missing=True,
        )
    )

    normalized_type = (
        normalize_cash_movement_type(
            movement_type
        )
    )

    normalized_amount = (
        normalize_positive_number(
            amount_usd,
            "amount_usd",
        )
    )

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cash_movements (
                    date,
                    time,
                    type,
                    amount_usd
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    normalized_date,
                    normalized_time,
                    normalized_type,
                    normalized_amount,
                ),
            )

            return int(
                cursor.lastrowid
            )

    return run_safe_mutation(
        table_name="cash_movements",
        operation="ADD",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def edit_cash_movement(
    cash_id: int,
    date: Any,
    movement_type: Any,
    amount_usd: Any,
    time_value: Any = None,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Edit one cash movement.
    """

    get_cash_movement(cash_id)

    normalized_date = (
        normalize_date_value(date)
    )

    normalized_time = (
        normalize_time_value(
            time_value,
            allow_missing=True,
        )
    )

    normalized_type = (
        normalize_cash_movement_type(
            movement_type
        )
    )

    normalized_amount = (
        normalize_positive_number(
            amount_usd,
            "amount_usd",
        )
    )

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE cash_movements
                SET
                    date = ?,
                    time = ?,
                    type = ?,
                    amount_usd = ?
                WHERE cash_id = ?
                """,
                (
                    normalized_date,
                    normalized_time,
                    normalized_type,
                    normalized_amount,
                    int(cash_id),
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Cash movement ID "
                    f"{cash_id} could not "
                    "be updated."
                )

            return int(cash_id)

    return run_safe_mutation(
        table_name="cash_movements",
        operation="EDIT",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def delete_cash_movement(
    cash_id: int,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Delete one cash movement.
    """

    get_cash_movement(cash_id)

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM cash_movements
                WHERE cash_id = ?
                """,
                (int(cash_id),),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Cash movement ID "
                    f"{cash_id} could not "
                    "be deleted."
                )

            return int(cash_id)

    return run_safe_mutation(
        table_name="cash_movements",
        operation="DELETE",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def list_dividends() -> pd.DataFrame:
    """
    Return all dividends.
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
            date,
            COALESCE(
                time,
                '00:00:00'
            ),
            dividend_id
        """
    )

    return rows_to_dataframe(rows)


def get_dividend(
    dividend_id: int,
) -> dict[str, Any]:
    """
    Return one dividend.
    """

    row = fetch_one(
        """
        SELECT
            dividend_id,
            date,
            time,
            ticker,
            net_dividend_usd
        FROM dividends
        WHERE dividend_id = ?
        """,
        (int(dividend_id),),
    )

    if row is None:
        raise ValueError(
            f"Dividend ID {dividend_id} "
            "does not exist."
        )

    return row


def add_dividend(
    date: Any,
    ticker: Any,
    net_dividend_usd: Any,
    time_value: Any = None,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Add one net dividend.
    """

    normalized_date = (
        normalize_date_value(date)
    )

    normalized_time = (
        normalize_time_value(
            time_value,
            allow_missing=True,
        )
    )

    normalized_ticker = (
        normalize_ticker(ticker)
    )

    normalized_amount = (
        normalize_non_negative_number(
            net_dividend_usd,
            "net_dividend_usd",
        )
    )

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO dividends (
                    date,
                    time,
                    ticker,
                    net_dividend_usd
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    normalized_date,
                    normalized_time,
                    normalized_ticker,
                    normalized_amount,
                ),
            )

            return int(
                cursor.lastrowid
            )

    return run_safe_mutation(
        table_name="dividends",
        operation="ADD",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def edit_dividend(
    dividend_id: int,
    date: Any,
    ticker: Any,
    net_dividend_usd: Any,
    time_value: Any = None,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Edit one net dividend.
    """

    get_dividend(dividend_id)

    normalized_date = (
        normalize_date_value(date)
    )

    normalized_time = (
        normalize_time_value(
            time_value,
            allow_missing=True,
        )
    )

    normalized_ticker = (
        normalize_ticker(ticker)
    )

    normalized_amount = (
        normalize_non_negative_number(
            net_dividend_usd,
            "net_dividend_usd",
        )
    )

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE dividends
                SET
                    date = ?,
                    time = ?,
                    ticker = ?,
                    net_dividend_usd = ?
                WHERE dividend_id = ?
                """,
                (
                    normalized_date,
                    normalized_time,
                    normalized_ticker,
                    normalized_amount,
                    int(dividend_id),
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Dividend ID "
                    f"{dividend_id} could "
                    "not be updated."
                )

            return int(
                dividend_id
            )

    return run_safe_mutation(
        table_name="dividends",
        operation="EDIT",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def delete_dividend(
    dividend_id: int,
    refresh_calculated_data: bool = True,
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> MutationResult:
    """
    Delete one dividend.
    """

    get_dividend(dividend_id)

    def mutation() -> int:
        with database_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM dividends
                WHERE dividend_id = ?
                """,
                (int(dividend_id),),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Dividend ID "
                    f"{dividend_id} could "
                    "not be deleted."
                )

            return int(
                dividend_id
            )

    return run_safe_mutation(
        table_name="dividends",
        operation="DELETE",
        mutation=mutation,
        refresh_calculated_data=(
            refresh_calculated_data
        ),
        progress_callback=(
            progress_callback
        ),
    )


def main() -> None:
    """
    Validate the transaction service without
    modifying portfolio data.
    """

    validate_database()

    trades = list_trades()
    cash_movements = (
        list_cash_movements()
    )
    dividends = list_dividends()

    print(
        "Transaction service is ready."
    )

    print(
        f"Trades: {len(trades)}"
    )

    print(
        "Cash movements: "
        f"{len(cash_movements)}"
    )

    print(
        f"Dividends: {len(dividends)}"
    )


if __name__ == "__main__":
    main()
