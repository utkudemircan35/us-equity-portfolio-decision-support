from argparse import ArgumentParser
from datetime import datetime, time
from pathlib import Path
from typing import Any
import pandas as pd
import yfinance as yf

from database import (
    PROJECT_DIR,
    database_transaction,
    validate_database,
)


DEFAULT_EXCEL_PATH = PROJECT_DIR / "portfolio.xlsx"

REQUIRED_SHEETS = {
    "trades",
    "cash_movements",
    "dividends",
}

TRADE_COLUMNS = [
    "date",
    "time",
    "ticker",
    "side",
    "shares",
    "price_usd",
    "commission_usd",
]

CASH_MOVEMENT_COLUMNS = [
    "date",
    "time",
    "type",
    "amount_usd",
]

DIVIDEND_COLUMNS = [
    "date",
    "time",
    "ticker",
    "net_dividend_usd",
]


def normalize_column_names(dataframe: pd.DataFrame) -> pd.DataFrame:
    """
    Remove leading and trailing spaces from Excel column names.
    """

    dataframe = dataframe.copy()
    dataframe.columns = [
        str(column).strip()
        for column in dataframe.columns
    ]

    return dataframe


def validate_required_columns(
    dataframe: pd.DataFrame,
    required_columns: list[str],
    sheet_name: str,
) -> None:
    """
    Validate that all required columns exist in an Excel sheet.
    """

    missing_columns = [
        column
        for column in required_columns
        if column not in dataframe.columns
    ]

    if missing_columns:
        missing_text = ", ".join(missing_columns)

        raise ValueError(
            f"Sheet '{sheet_name}' is missing required columns: "
            f"{missing_text}"
        )


def normalize_date_series(
    series: pd.Series,
    sheet_name: str,
) -> pd.Series:
    """
    Convert Excel dates to ISO date strings.
    """

    converted = pd.to_datetime(
        series,
        errors="coerce",
    )

    invalid_rows = converted.isna()

    if invalid_rows.any():
        row_numbers = (
            invalid_rows[invalid_rows]
            .index.to_series()
            .add(2)
            .astype(str)
            .tolist()
        )

        raise ValueError(
            f"Invalid date value in sheet '{sheet_name}' "
            f"at Excel rows: {', '.join(row_numbers)}"
        )

    return converted.dt.strftime("%Y-%m-%d")


def normalize_time_value(
    value: Any,
    allow_missing: bool = False,
) -> str | None:
    """
    Convert an Excel time value to an HH:MM:SS string.
    """

    if pd.isna(value):
        if allow_missing:
            return None

        raise ValueError("Time value cannot be empty.")

    if isinstance(value, time):
        return value.strftime("%H:%M:%S")

    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")

    if isinstance(value, pd.Timestamp):
        return value.strftime("%H:%M:%S")

    # Excel may store time as a fraction of one day.
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value < 1
    ):
        total_seconds = int(float(value) * 86_400 + 0.5)
        total_seconds %= 86_400

        hours = total_seconds // 3_600
        minutes = (total_seconds % 3_600) // 60
        seconds = total_seconds % 60

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    text_value = str(value).strip()

    if not text_value:
        if allow_missing:
            return None

        raise ValueError("Time value cannot be empty.")

    parsed_time = pd.to_datetime(
        text_value,
        errors="coerce",
    )

    if pd.isna(parsed_time):
        raise ValueError(f"Invalid time value: {value}")

    return parsed_time.strftime("%H:%M:%S")


def normalize_time_series(
    series: pd.Series,
    sheet_name: str,
    allow_missing: bool = False,
) -> pd.Series:
    """
    Normalize all time values and report the source row on failure.
    """

    normalized_values: list[str | None] = []

    for index, value in series.items():
        try:
            normalized_value = normalize_time_value(
                value,
                allow_missing=allow_missing,
            )

        except ValueError as error:
            excel_row = index + 2

            raise ValueError(
                f"Invalid time in sheet '{sheet_name}' "
                f"at Excel row {excel_row}: {error}"
            ) from error

        normalized_values.append(normalized_value)

    return pd.Series(
        normalized_values,
        index=series.index,
        dtype="object",
    )


def convert_numeric_column(
    dataframe: pd.DataFrame,
    column_name: str,
    sheet_name: str,
) -> None:
    """
    Convert a column to numeric values without rounding.
    """

    original_values = dataframe[column_name]

    converted_values = pd.to_numeric(
        original_values,
        errors="coerce",
    )

    invalid_rows = (
        original_values.isna()
        | converted_values.isna()
    )

    if invalid_rows.any():
        row_numbers = (
            invalid_rows[invalid_rows]
            .index.to_series()
            .add(2)
            .astype(str)
            .tolist()
        )

        raise ValueError(
            f"Invalid numeric value in column '{column_name}' "
            f"of sheet '{sheet_name}' at Excel rows: "
            f"{', '.join(row_numbers)}"
        )

    dataframe[column_name] = converted_values


def normalize_boolean_value(value: Any) -> bool:
    """
    Convert an optional Excel value to a boolean value.
    """

    if pd.isna(value):
        return False

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        if value == 1:
            return True

        if value == 0:
            return False

    normalized_value = str(value).strip().lower()

    true_values = {
        "true",
        "yes",
        "y",
        "1",
    }

    false_values = {
        "false",
        "no",
        "n",
        "0",
        "",
    }

    if normalized_value in true_values:
        return True

    if normalized_value in false_values:
        return False

    raise ValueError(
        f"Invalid is_gift value: {value}"
    )

def download_split_history(
    tickers: list[str],
) -> dict[str, list[tuple[pd.Timestamp, float]]]:
    """
    Download stock split history for transaction tickers.
    """

    split_history: dict[
        str,
        list[tuple[pd.Timestamp, float]],
    ] = {}

    for ticker in sorted(set(tickers)):
        print(f"Checking stock splits: {ticker}")

        try:
            splits = yf.Ticker(
                ticker
            ).get_splits(
                period="max"
            )

        except Exception as error:
            print(
                f"Stock split data could not be downloaded "
                f"for {ticker}: {error}"
            )

            split_history[ticker] = []
            continue

        if splits is None or splits.empty:
            split_history[ticker] = []
            continue

        ticker_splits: list[
            tuple[pd.Timestamp, float]
        ] = []

        for split_datetime, split_ratio in (
            splits.items()
        ):
            split_date = pd.Timestamp(
                split_datetime
            )

            if split_date.tzinfo is not None:
                split_date = split_date.tz_localize(
                    None
                )

            ratio = float(split_ratio)

            if ratio <= 0:
                continue

            if ratio == 1.0:
                continue

            ticker_splits.append(
                (
                    split_date.normalize(),
                    ratio,
                )
            )

        ticker_splits.sort(
            key=lambda item: item[0]
        )

        split_history[ticker] = ticker_splits

        if ticker_splits:
            split_text = ", ".join(
                f"{split_date.date()} ({ratio}:1)"
                for split_date, ratio
                in ticker_splits
            )

            print(
                f"{ticker} stock splits: "
                f"{split_text}"
            )

    return split_history


def calculate_split_factor(
    ticker: str,
    trade_date: pd.Timestamp,
    split_history: dict[
        str,
        list[tuple[pd.Timestamp, float]],
    ],
) -> float:
    """
    Calculate the cumulative split factor after a trade date.
    """

    factor = 1.0

    for split_date, split_ratio in (
        split_history.get(ticker, [])
    ):
        # Transactions on the effective split date are
        # already expressed in post-split units.
        if split_date > trade_date:
            factor *= split_ratio

    return factor


def apply_stock_split_adjustments(
    trades: pd.DataFrame,
) -> pd.DataFrame:
    """
    Convert historical trades to the latest share unit.
    """

    adjusted_trades = trades.copy()

    tickers = (
        adjusted_trades["ticker"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
        .tolist()
    )

    split_history = download_split_history(
        tickers
    )

    trade_dates = pd.to_datetime(
        adjusted_trades["date"],
        errors="raise",
    ).dt.normalize()

    split_factors: list[float] = []

    for index, row in adjusted_trades.iterrows():
        ticker = row["ticker"]
        trade_date = trade_dates.loc[index]

        factor = calculate_split_factor(
            ticker=ticker,
            trade_date=trade_date,
            split_history=split_history,
        )

        split_factors.append(factor)

    adjusted_trades["_split_factor"] = (
        split_factors
    )

    affected_rows = (
        adjusted_trades["_split_factor"] != 1.0
    )

    if affected_rows.any():
        print(
            "Applying stock split adjustments "
            f"to {affected_rows.sum()} trade rows."
        )

    # Share quantities are multiplied by the split factor.
    adjusted_trades["shares"] = (
        adjusted_trades["shares"]
        * adjusted_trades["_split_factor"]
    )

    # Prices are divided by the same factor.
    adjusted_trades["price_usd"] = (
        adjusted_trades["price_usd"]
        / adjusted_trades["_split_factor"]
    )

    return adjusted_trades

def prepare_trades(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    """
    Validate trades and calculate net flow and cumulative shares.
    """

    trades = normalize_column_names(dataframe)

    validate_required_columns(
        trades,
        TRADE_COLUMNS,
        "trades",
    )

    # Excel IDs and previously calculated columns are deliberately ignored.
    selected_columns = TRADE_COLUMNS.copy()

    if "is_gift" in trades.columns:
        selected_columns.append("is_gift")

    trades = trades[selected_columns].copy()

    trades["date"] = normalize_date_series(
        trades["date"],
        "trades",
    )

    trades["time"] = normalize_time_series(
        trades["time"],
        "trades",
        allow_missing=True,
    )

    trades["ticker"] = (
        trades["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    if (trades["ticker"] == "").any():
        raise ValueError(
            "Ticker cannot be empty in sheet 'trades'."
        )

    trades["side"] = (
        trades["side"]
        .astype(str)
        .str.strip()
        .str.title()
    )

    invalid_sides = ~trades["side"].isin(
        ["Buy", "Sell"]
    )

    if invalid_sides.any():
        invalid_values = sorted(
            trades.loc[invalid_sides, "side"]
            .astype(str)
            .unique()
            .tolist()
        )

        raise ValueError(
            "Invalid side values in sheet 'trades': "
            + ", ".join(invalid_values)
        )

    numeric_columns = [
        "shares",
        "price_usd",
        "commission_usd",
    ]

    for column_name in numeric_columns:
        convert_numeric_column(
            trades,
            column_name,
            "trades",
        )

    if (trades["shares"] <= 0).any():
        raise ValueError(
            "All trade share values must be greater than zero."
        )

    if (trades["price_usd"] < 0).any():
        raise ValueError(
            "Trade prices cannot be negative."
        )

    if (trades["commission_usd"] < 0).any():
        raise ValueError(
            "Trade commissions cannot be negative."
        )

    # Preserve the exact values entered by the user.
    # These values must never be split-adjusted.
    trades["raw_shares"] = (
        trades["shares"].copy()
    )

    trades["raw_price_usd"] = (
        trades["price_usd"].copy()
    )

    trades = apply_stock_split_adjustments(
        trades
    )

    trades["split_factor"] = (
        trades["_split_factor"]
    )

    if "is_gift" in trades.columns:
        gift_values: list[bool] = []

        for index, value in trades["is_gift"].items():
            try:
                gift_values.append(
                    normalize_boolean_value(value)
                )

            except ValueError as error:
                excel_row = index + 2

                raise ValueError(
                    f"Invalid is_gift value in sheet 'trades' "
                    f"at Excel row {excel_row}: {error}"
                ) from error

        trades["is_gift"] = gift_values

    else:
        trades["is_gift"] = False

    invalid_gift_rows = (
        trades["is_gift"]
        & (trades["side"] != "Buy")
    )

    if invalid_gift_rows.any():
        raise ValueError(
            "A gift transaction must use 'Buy' as its side."
        )

    invalid_gift_commissions = (
        trades["is_gift"]
        & (
            trades["commission_usd"]
            > 1e-10
        )
    )

    if invalid_gift_commissions.any():
        invalid_rows = (
            invalid_gift_commissions[
                invalid_gift_commissions
            ]
            .index.to_series()
            .add(2)
            .astype(str)
            .tolist()
        )

        raise ValueError(
            "Gift transactions cannot have "
            "a commission. Excel rows: "
            + ", ".join(invalid_rows)
        )

    # Keep the original Excel order when date and time are identical.
    trades["_source_order"] = range(len(trades))

    trades["_sort_time"] = trades["time"].fillna("00:00:00")

    trades["_datetime"] = pd.to_datetime(
        trades["date"] + " " + trades["_sort_time"],
        errors="raise",
    )

    trades = (
        trades.sort_values(
            by=["_datetime", "_source_order"],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    # Calculate cash flow without applying any rounding.
    gross_values = (
        trades["shares"]
        * trades["price_usd"]
    )

    buy_rows = trades["side"] == "Buy"
    sell_rows = trades["side"] == "Sell"

    trades["net_flow_usd"] = 0.0

    trades.loc[buy_rows, "net_flow_usd"] = -(
        gross_values[buy_rows]
        + trades.loc[buy_rows, "commission_usd"]
    )

    trades.loc[sell_rows, "net_flow_usd"] = (
        gross_values[sell_rows]
        - trades.loc[sell_rows, "commission_usd"]
    )

    invalid_sell_cash_flows = (
        sell_rows
        & (
            trades["net_flow_usd"]
            < -1e-10
        )
    )

    if invalid_sell_cash_flows.any():
        invalid_rows = (
            trades.loc[
                invalid_sell_cash_flows,
                "_source_order",
            ]
            .add(2)
            .astype(str)
            .tolist()
        )

        raise ValueError(
            "Sell commission cannot exceed "
            "the gross sale value. Excel rows: "
            + ", ".join(invalid_rows)
        )

    # Gift shares increase the position but do not create a cash flow.
    trades.loc[
        trades["is_gift"],
        "net_flow_usd",
    ] = 0.0

    # Buy transactions increase shares and sell transactions reduce shares.
    trades["_share_change"] = trades["shares"]

    trades.loc[
        sell_rows,
        "_share_change",
    ] = -trades.loc[sell_rows, "shares"]

    # Calculate the running share balance separately for every ticker.
    trades["cumulative_shares"] = (
        trades.groupby(
            "ticker",
            sort=False,
        )["_share_change"]
        .cumsum()
    )

    negative_balances = (
        trades["cumulative_shares"]
        < -1e-8
    )

    if negative_balances.any():
        invalid_row = trades.loc[
            negative_balances
        ].iloc[0]

        excel_row = int(
            invalid_row["_source_order"]
        ) + 2

        raise ValueError(
            "Trade history cannot produce "
            "a negative share balance. "
            f"Ticker: {invalid_row['ticker']}, "
            f"Excel row: {excel_row}."
        )

    final_columns = [
        "date",
        "time",
        "ticker",
        "side",
        "raw_shares",
        "raw_price_usd",
        "split_factor",
        "is_gift",
        "shares",
        "price_usd",
        "commission_usd",
        "net_flow_usd",
        "cumulative_shares",
    ]

    return trades[final_columns].copy()


def prepare_cash_movements(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    """
    Validate and prepare cash movements.
    """

    cash_movements = normalize_column_names(
        dataframe
    )

    validate_required_columns(
        cash_movements,
        CASH_MOVEMENT_COLUMNS,
        "cash_movements",
    )

    # Excel cash IDs are deliberately ignored.
    cash_movements = cash_movements[
        CASH_MOVEMENT_COLUMNS
    ].copy()

    cash_movements["date"] = normalize_date_series(
        cash_movements["date"],
        "cash_movements",
    )

    cash_movements["time"] = normalize_time_series(
        cash_movements["time"],
        "cash_movements",
        allow_missing=True,
    )

    cash_movements["type"] = (
        cash_movements["type"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    allowed_types = {
        "DEPOSIT",
        "WITHDRAWAL",
        "INTEREST",
    }

    invalid_types = ~cash_movements["type"].isin(
        allowed_types
    )

    if invalid_types.any():
        invalid_values = sorted(
            cash_movements.loc[
                invalid_types,
                "type",
            ]
            .astype(str)
            .unique()
            .tolist()
        )

        raise ValueError(
            "Invalid cash movement types: "
            + ", ".join(invalid_values)
        )

    convert_numeric_column(
        cash_movements,
        "amount_usd",
        "cash_movements",
    )

    if (cash_movements["amount_usd"] <= 0).any():
        raise ValueError(
            "Cash movement amounts must be greater than zero."
        )

    cash_movements["_source_order"] = range(
        len(cash_movements)
    )

    # Missing times are treated as the beginning of the day for sorting only.
    cash_movements["_sort_time"] = (
        cash_movements["time"].fillna("00:00:00")
    )

    cash_movements["_datetime"] = pd.to_datetime(
        cash_movements["date"]
        + " "
        + cash_movements["_sort_time"],
        errors="raise",
    )

    cash_movements = (
        cash_movements.sort_values(
            by=["_datetime", "_source_order"],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    return cash_movements[
        CASH_MOVEMENT_COLUMNS
    ].copy()


def prepare_dividends(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    """
    Validate and prepare dividend transactions.
    """

    dividends = normalize_column_names(dataframe)

    validate_required_columns(
        dividends,
        DIVIDEND_COLUMNS,
        "dividends",
    )

    # Excel dividend IDs are deliberately ignored.
    dividends = dividends[
        DIVIDEND_COLUMNS
    ].copy()

    dividends["date"] = normalize_date_series(
        dividends["date"],
        "dividends",
    )

    dividends["time"] = normalize_time_series(
        dividends["time"],
        "dividends",
        allow_missing=True,
    )

    dividends["ticker"] = (
        dividends["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    if (dividends["ticker"] == "").any():
        raise ValueError(
            "Ticker cannot be empty in sheet 'dividends'."
        )

    convert_numeric_column(
        dividends,
        "net_dividend_usd",
        "dividends",
    )

    if (dividends["net_dividend_usd"] < 0).any():
        raise ValueError(
            "Net dividend values cannot be negative."
        )

    dividends["_source_order"] = range(
        len(dividends)
    )

    dividends = (
        dividends.sort_values(
            by=["date", "_source_order"],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    return dividends[
        DIVIDEND_COLUMNS
    ].copy()


def read_portfolio_excel(
    excel_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Read and prepare the three transaction sheets.
    """

    if not excel_path.exists():
        raise FileNotFoundError(
            f"Excel file does not exist: {excel_path}"
        )

    excel_file = pd.ExcelFile(
        excel_path,
        engine="openpyxl",
    )

    available_sheets = set(excel_file.sheet_names)

    missing_sheets = REQUIRED_SHEETS - available_sheets

    if missing_sheets:
        raise ValueError(
            "Excel file is missing required sheets: "
            + ", ".join(sorted(missing_sheets))
        )

    trades_raw = pd.read_excel(
        excel_file,
        sheet_name="trades",
        engine="openpyxl",
    )

    cash_movements_raw = pd.read_excel(
        excel_file,
        sheet_name="cash_movements",
        engine="openpyxl",
    )

    dividends_raw = pd.read_excel(
        excel_file,
        sheet_name="dividends",
        engine="openpyxl",
    )

    trades = prepare_trades(trades_raw)

    cash_movements = prepare_cash_movements(
        cash_movements_raw
    )

    dividends = prepare_dividends(dividends_raw)

    return trades, cash_movements, dividends


def raw_tables_have_data(connection) -> bool:
    """
    Check whether any transaction table already contains data.
    """

    table_names = [
        "trades",
        "cash_movements",
        "dividends",
    ]

    for table_name in table_names:
        row = connection.execute(
            f"""
            SELECT EXISTS (
                SELECT 1
                FROM {table_name}
                LIMIT 1
            ) AS has_data
            """
        ).fetchone()

        if row["has_data"]:
            return True

    return False


def clear_existing_portfolio_data(connection) -> None:
    """
    Remove existing imported and calculated portfolio data.
    """
    connection.execute(
        "DELETE FROM security_daily"
    )
    connection.execute(
        "DELETE FROM holdings_summary"
    )

    connection.execute(
        "DELETE FROM portfolio_daily"
    )

    connection.execute(
        "DELETE FROM market_prices"
    )

    connection.execute(
        "DELETE FROM dividends"
    )

    connection.execute(
        "DELETE FROM cash_movements"
    )

    connection.execute(
        "DELETE FROM trades"
    )

    connection.execute(
        """
        DELETE FROM sqlite_sequence
        WHERE name IN (
            'trades',
            'cash_movements',
            'dividends'
        )
        """
    )


def insert_trades(
    connection,
    trades: pd.DataFrame,
) -> None:
    """
    Insert prepared trades into SQLite.
    """

    query = """
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
    """

    rows = [
        (
            row.date,
            row.time,
            row.ticker,
            row.side,
            float(
                row.raw_shares
            ),
            float(
                row.raw_price_usd
            ),
            float(
                row.split_factor
            ),
            int(
                bool(row.is_gift)
            ),
            float(
                row.shares
            ),
            float(
                row.price_usd
            ),
            float(
                row.commission_usd
            ),
            float(
                row.net_flow_usd
            ),
            float(
                row.cumulative_shares
            ),
        )
        for row in trades.itertuples(
            index=False
        )
    ]

    connection.executemany(
        query,
        rows,
    )

def insert_cash_movements(
    connection,
    cash_movements: pd.DataFrame,
) -> None:
    """
    Insert prepared cash movements into SQLite.
    """

    query = """
        INSERT INTO cash_movements (
            date,
            time,
            type,
            amount_usd
        )
        VALUES (?, ?, ?, ?)
    """

    rows = [
        (
            row.date,
            row.time,
            row.type,
            float(row.amount_usd),
        )
        for row in cash_movements.itertuples(
            index=False
        )
    ]

    connection.executemany(query, rows)


def insert_dividends(
    connection,
    dividends: pd.DataFrame,
) -> None:
    """
    Insert prepared dividends into SQLite.
    """

    query = """
        INSERT INTO dividends (
            date,
            time,
            ticker,
            net_dividend_usd
        )
        VALUES (?, ?, ?, ?)
    """

    rows = [
        (
            row.date,
            row.time,
            row.ticker,
            row.net_dividend_usd
            if row.time is None
            else float(row.net_dividend_usd),
        )
        for row in dividends.itertuples(index=False)
    ]

    # Ensure dividend amounts are always stored as numeric values.
    rows = [
        (
            date,
            time_value,
            ticker,
            float(net_dividend_usd),
        )
        for (
            date,
            time_value,
            ticker,
            net_dividend_usd,
        ) in rows
    ]

    connection.executemany(query, rows)


def import_excel_to_database(
    excel_path: Path = DEFAULT_EXCEL_PATH,
    replace_existing: bool = False,
) -> dict[str, int]:
    """
    Import portfolio transactions from Excel into SQLite.
    """

    validate_database()

    trades, cash_movements, dividends = (
        read_portfolio_excel(excel_path)
    )

    with database_transaction() as connection:
        if raw_tables_have_data(connection):
            if not replace_existing:
                raise RuntimeError(
                    "The database already contains transaction data. "
                    "Use --replace-existing only if the Excel file "
                    "contains the complete portfolio history."
                )

            clear_existing_portfolio_data(connection)

        insert_trades(connection, trades)

        insert_cash_movements(
            connection,
            cash_movements,
        )

        insert_dividends(
            connection,
            dividends,
        )

    return {
        "trades": len(trades),
        "cash_movements": len(cash_movements),
        "dividends": len(dividends),
    }


def build_argument_parser() -> ArgumentParser:
    """
    Build command-line arguments.
    """

    parser = ArgumentParser(
        description=(
            "Import portfolio transactions "
            "from Excel into SQLite."
        )
    )

    parser.add_argument(
        "excel_path",
        nargs="?",
        type=Path,
        default=DEFAULT_EXCEL_PATH,
        help=(
            "Path to the portfolio Excel file. "
            "Defaults to portfolio.xlsx "
            "in the project directory."
        ),
    )

    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "Delete existing portfolio data "
            "before importing the Excel file."
        ),
    )

    return parser


def main() -> None:
    """
    Run the Excel import process.
    """

    parser = build_argument_parser()
    arguments = parser.parse_args()

    imported_rows = import_excel_to_database(
        excel_path=arguments.excel_path,
        replace_existing=arguments.replace_existing,
    )

    print("Excel import completed successfully.")
    print(f"Trades imported: {imported_rows['trades']}")
    print(
        "Cash movements imported: "
        f"{imported_rows['cash_movements']}"
    )
    print(
        f"Dividends imported: "
        f"{imported_rows['dividends']}"
    )


if __name__ == "__main__":
    main()
