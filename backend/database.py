from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Sequence


# Project paths
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
DATABASE_PATH = PROJECT_DIR / "portfolio.db"


# Expected database structure
EXPECTED_TABLE_COLUMNS = {
    "trades": (
        "trade_id",
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
    ),
    "cash_movements": (
        "cash_id",
        "date",
        "time",
        "type",
        "amount_usd",
    ),
    "dividends": (
        "dividend_id",
        "date",
        "time",
        "ticker",
        "net_dividend_usd",
    ),
    "assets": (
        "ticker",
        "company_name",
        "asset_type",
        "sector",
        "industry",
    ),
    "holdings_summary": (
        "ticker",
        "as_of_date",
        "status",
        "shares",
        "price_usd",
        "market_value_usd",
        "weight",
        "remaining_cost_basis_usd",
        "average_cost_usd",
        "realized_pnl_usd",
        "unrealized_pnl_usd",
    ),
    "portfolio_daily": (
        "date",
        "opening_value_usd",
        "securities_value_usd",
        "cash_balance_usd",
        "closing_value_usd",
        "external_inflow_usd",
        "external_outflow_usd",
        "daily_return",
        "cumulative_twr",
    ),
    "security_daily": (
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
    ),
    "market_prices": (
        "key",
        "ticker",
        "date",
        "close_usd",
        "adjusted_close_usd",
    ),
}


def get_connection() -> sqlite3.Connection:
    """
    Create and return a configured SQLite connection.
    """

    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")

    return connection


@contextmanager
def database_transaction() -> Iterator[sqlite3.Connection]:
    """
    Open a database transaction.

    The transaction is committed when the block completes successfully.
    It is rolled back if an exception occurs.
    """

    connection = get_connection()

    try:
        yield connection
        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def fetch_all(
    query: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """
    Execute a SELECT query and return all rows as dictionaries.
    """

    connection = get_connection()

    try:
        cursor = connection.execute(query, parameters)
        return [dict(row) for row in cursor.fetchall()]

    finally:
        connection.close()


def fetch_one(
    query: str,
    parameters: Sequence[Any] = (),
) -> dict[str, Any] | None:
    """
    Execute a SELECT query and return one row as a dictionary.
    """

    connection = get_connection()

    try:
        cursor = connection.execute(query, parameters)
        row = cursor.fetchone()

        if row is None:
            return None

        return dict(row)

    finally:
        connection.close()


def execute_write(
    query: str,
    parameters: Sequence[Any] = (),
) -> int:
    """
    Execute a single INSERT, UPDATE or DELETE statement.

    Returns the last inserted row ID.
    """

    with database_transaction() as connection:
        cursor = connection.execute(query, parameters)
        return cursor.lastrowid


def execute_many(
    query: str,
    parameter_rows: Iterable[Sequence[Any]],
) -> int:
    """
    Execute the same write statement for multiple rows.

    Returns the number of affected rows when available.
    """

    with database_transaction() as connection:
        cursor = connection.executemany(query, parameter_rows)
        return cursor.rowcount


def get_existing_tables(
    connection: sqlite3.Connection | None = None,
) -> set[str]:
    """
    Return the names of all user-created database tables.
    """

    owns_connection = connection is None

    if connection is None:
        connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        return {row["name"] for row in rows}

    finally:
        if owns_connection:
            connection.close()


def get_table_columns(
    table_name: str,
    connection: sqlite3.Connection | None = None,
) -> tuple[str, ...]:
    """
    Return the column names of a database table.
    """

    if table_name not in EXPECTED_TABLE_COLUMNS:
        raise ValueError(f"Unknown table: {table_name}")

    owns_connection = connection is None

    if connection is None:
        connection = get_connection()

    try:
        rows = connection.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()

        return tuple(row["name"] for row in rows)

    finally:
        if owns_connection:
            connection.close()


def get_schema_issues() -> dict[str, Any]:
    """
    Compare the current database structure with the expected structure.
    """

    if not DATABASE_PATH.exists():
        return {
            "database_exists": False,
            "missing_tables": list(EXPECTED_TABLE_COLUMNS),
            "table_issues": {},
        }

    connection = get_connection()

    try:
        existing_tables = get_existing_tables(connection)

        missing_tables = [
            table_name
            for table_name in EXPECTED_TABLE_COLUMNS
            if table_name not in existing_tables
        ]

        table_issues: dict[str, dict[str, list[str]]] = {}

        for table_name, expected_columns in EXPECTED_TABLE_COLUMNS.items():
            if table_name not in existing_tables:
                continue

            actual_columns = get_table_columns(
                table_name,
                connection,
            )

            missing_columns = [
                column
                for column in expected_columns
                if column not in actual_columns
            ]

            unexpected_columns = [
                column
                for column in actual_columns
                if column not in expected_columns
            ]

            if missing_columns or unexpected_columns:
                table_issues[table_name] = {
                    "missing_columns": missing_columns,
                    "unexpected_columns": unexpected_columns,
                }

        return {
            "database_exists": True,
            "missing_tables": missing_tables,
            "table_issues": table_issues,
        }

    finally:
        connection.close()


def validate_database() -> None:
    """
    Validate that all required tables and columns exist.
    """

    issues = get_schema_issues()

    if not issues["database_exists"]:
        raise RuntimeError(
            f"Database does not exist: {DATABASE_PATH}"
        )

    messages: list[str] = []

    if issues["missing_tables"]:
        missing = ", ".join(issues["missing_tables"])
        messages.append(f"Missing tables: {missing}")

    for table_name, table_issue in issues["table_issues"].items():
        missing_columns = table_issue["missing_columns"]
        unexpected_columns = table_issue["unexpected_columns"]

        if missing_columns:
            messages.append(
                f"{table_name} is missing columns: "
                + ", ".join(missing_columns)
            )

        if unexpected_columns:
            messages.append(
                f"{table_name} has unexpected columns: "
                + ", ".join(unexpected_columns)
            )

    if messages:
        raise RuntimeError(
            "Invalid database structure:\n- "
            + "\n- ".join(messages)
        )


def main() -> None:
    """
    Check the database connection and database structure.
    """

    print(f"Database path: {DATABASE_PATH}")

    try:
        validate_database()

    except RuntimeError as error:
        print(error)
        return

    connection = get_connection()

    try:
        connection.execute("SELECT 1")
        print("Database connection: OK")
        print("Database structure: OK")

        for table_name in EXPECTED_TABLE_COLUMNS:
            row = connection.execute(
                f'SELECT COUNT(*) AS row_count FROM "{table_name}"'
            ).fetchone()

            print(f"{table_name}: {row['row_count']} rows")

    finally:
        connection.close()


if __name__ == "__main__":
    main()
