from database import (
    DATABASE_PATH,
    get_connection,
    validate_database,
)


def create_trades_table(connection) -> None:
    """
    Create the trades table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,

            date TEXT NOT NULL,
            time TEXT,
            ticker TEXT NOT NULL,

            side TEXT NOT NULL
                CHECK (
                    side IN (
                        'Buy',
                        'Sell'
                    )
                ),

            raw_shares REAL NOT NULL
                CHECK (raw_shares > 0),

            raw_price_usd REAL NOT NULL
                CHECK (raw_price_usd >= 0),

            split_factor REAL NOT NULL
                DEFAULT 1.0
                CHECK (split_factor > 0),

            is_gift INTEGER NOT NULL
                DEFAULT 0
                CHECK (is_gift IN (0, 1)),

            shares REAL NOT NULL
                CHECK (shares > 0),

            price_usd REAL NOT NULL
                CHECK (price_usd >= 0),

            commission_usd REAL NOT NULL
                DEFAULT 0
                CHECK (commission_usd >= 0),

            net_flow_usd REAL NOT NULL,

            cumulative_shares REAL NOT NULL,

            CHECK (
                (
                    side = 'Buy'
                    AND net_flow_usd <= 0
                )
                OR
                (
                    side = 'Sell'
                    AND net_flow_usd >= 0
                )
            ),

            CHECK (
                is_gift = 0
                OR side = 'Buy'
            ),

            CHECK (
                is_gift = 0
                OR net_flow_usd = 0
            )
        )
        """
    )

def migrate_trades_table(connection) -> None:
    """
    Add raw trade fields to an existing trades table.

    Existing calculated values are used as temporary raw values.
    A complete Excel re-import will later restore the true raw
    values and split factors.
    """

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(trades)"
        ).fetchall()
    }

    added_columns: list[str] = []

    if "raw_shares" not in columns:
        connection.execute(
            """
            ALTER TABLE trades
            ADD COLUMN raw_shares REAL NOT NULL
            DEFAULT 0
            CHECK (raw_shares >= 0)
            """
        )

        added_columns.append(
            "raw_shares"
        )

    if "raw_price_usd" not in columns:
        connection.execute(
            """
            ALTER TABLE trades
            ADD COLUMN raw_price_usd REAL NOT NULL
            DEFAULT 0
            CHECK (raw_price_usd >= 0)
            """
        )

        added_columns.append(
            "raw_price_usd"
        )

    if "split_factor" not in columns:
        connection.execute(
            """
            ALTER TABLE trades
            ADD COLUMN split_factor REAL NOT NULL
            DEFAULT 1.0
            CHECK (split_factor > 0)
            """
        )

        added_columns.append(
            "split_factor"
        )

    if "is_gift" not in columns:
        connection.execute(
            """
            ALTER TABLE trades
            ADD COLUMN is_gift INTEGER NOT NULL
            DEFAULT 0
            CHECK (is_gift IN (0, 1))
            """
        )

        added_columns.append(
            "is_gift"
        )

    # Existing rows temporarily use their current adjusted
    # values as raw values. A complete Excel re-import will
    # replace these with the actual source values.
    connection.execute(
        """
        UPDATE trades
        SET raw_shares = shares
        WHERE raw_shares = 0
        """
    )

    connection.execute(
        """
        UPDATE trades
        SET raw_price_usd = price_usd
        WHERE raw_price_usd = 0
          AND price_usd <> 0
        """
    )

    connection.execute(
        """
        UPDATE trades
        SET split_factor = 1.0
        WHERE split_factor IS NULL
           OR split_factor <= 0
        """
    )

    # Recover existing gift rows from their zero cash flow.
    connection.execute(
        """
        UPDATE trades
        SET is_gift = 1
        WHERE side = 'Buy'
          AND ABS(net_flow_usd) <= 0.0000000001
        """
    )

    if added_columns:
        print(
            "Database migration completed: "
            + ", ".join(added_columns)
            + " added to trades."
        )

def create_cash_movements_table(connection) -> None:
    """
    Create the cash movements table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cash_movements (
            cash_id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT,
            type TEXT NOT NULL
                CHECK (
                    type IN (
                        'DEPOSIT',
                        'WITHDRAWAL',
                        'INTEREST'
                    )
                ),
            amount_usd REAL NOT NULL
                CHECK (amount_usd > 0)
        )
        """
    )


def create_dividends_table(connection) -> None:
    """
    Create the dividends table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dividends (
            dividend_id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT,
            ticker TEXT NOT NULL,
            net_dividend_usd REAL NOT NULL
                CHECK (net_dividend_usd >= 0)
        )
        """
    )

def create_assets_table(connection) -> None:
    """
    Create the asset metadata table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            ticker TEXT PRIMARY KEY,
            company_name TEXT NOT NULL,
            asset_type TEXT NOT NULL
                CHECK (
                    asset_type IN (
                        'EQUITY',
                        'ETF',
                        'CASH',
                        'UNKNOWN'
                    )
                ),
            sector TEXT NOT NULL,
            industry TEXT NOT NULL
        )
        """
    )

def create_holdings_summary_table(connection) -> None:
    """
    Create the calculated holdings summary table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS holdings_summary (
            ticker TEXT PRIMARY KEY,
            as_of_date TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('OPEN', 'CLOSED')),
            shares REAL NOT NULL,
            price_usd REAL NOT NULL
                CHECK (price_usd >= 0),
            market_value_usd REAL NOT NULL,
            weight REAL NOT NULL
                CHECK (weight >= 0),
            remaining_cost_basis_usd REAL NOT NULL,
            average_cost_usd REAL NOT NULL,
            realized_pnl_usd REAL NOT NULL,
            unrealized_pnl_usd REAL NOT NULL
        )
        """
    )


def create_portfolio_daily_table(connection) -> None:
    """
    Create the daily portfolio performance table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_daily (
            date TEXT PRIMARY KEY,
            opening_value_usd REAL NOT NULL,
            securities_value_usd REAL NOT NULL,
            cash_balance_usd REAL NOT NULL,
            closing_value_usd REAL NOT NULL,
            external_inflow_usd REAL NOT NULL,
            external_outflow_usd REAL NOT NULL,
            daily_return REAL NOT NULL,
            cumulative_twr REAL NOT NULL DEFAULT 0.0
        )
        """
    )

def migrate_portfolio_daily_table(connection) -> None:
    """
    Add cumulative_twr to an existing portfolio_daily table.
    """

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(portfolio_daily)"
        ).fetchall()
    }

    if "cumulative_twr" not in columns:
        connection.execute(
            """
            ALTER TABLE portfolio_daily
            ADD COLUMN cumulative_twr REAL NOT NULL DEFAULT 0.0
            """
        )

        print(
            "Database migration completed: "
            "cumulative_twr added to portfolio_daily."
        )

def create_security_daily_table(connection) -> None:
    """
    Create the daily security performance table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS security_daily (
            key TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,

            opening_shares REAL NOT NULL
                CHECK (opening_shares >= 0),

            closing_shares REAL NOT NULL
                CHECK (closing_shares >= 0),

            close_usd REAL NOT NULL
                CHECK (close_usd >= 0),

            opening_value_usd REAL NOT NULL
                CHECK (opening_value_usd >= 0),

            closing_value_usd REAL NOT NULL
                CHECK (closing_value_usd >= 0),

            trade_flow_usd REAL NOT NULL,
            dividend_usd REAL NOT NULL
                CHECK (dividend_usd >= 0),

            daily_return REAL NOT NULL,
            cumulative_twr REAL NOT NULL,

            UNIQUE (ticker, date)
        )
        """
    )

def create_market_prices_table(connection) -> None:
    """
    Create the historical market prices table.
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_prices (
            key TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            close_usd REAL NOT NULL
                CHECK (close_usd >= 0),
            adjusted_close_usd REAL NOT NULL
                CHECK (adjusted_close_usd >= 0),

            UNIQUE (ticker, date)
        )
        """
    )


def create_indexes(connection) -> None:
    """
    Create indexes used by portfolio calculations and filters.
    """

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trades_date_time
        ON trades (date, time)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trades_ticker_date_time
        ON trades (ticker, date, time)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cash_movements_date_time
        ON cash_movements (date, time)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cash_movements_type
        ON cash_movements (type)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dividends_ticker_date
        ON dividends (ticker, date)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_prices_date
        ON market_prices (date)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_security_daily_ticker_date
        ON security_daily (ticker, date)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_security_daily_date
        ON security_daily (date)
        """
    )


def create_database() -> None:
    """
    Create the portfolio database and all required tables.
    """

    connection = get_connection()

    try:
        create_trades_table(connection)
        migrate_trades_table(connection)
        create_cash_movements_table(connection)
        create_dividends_table(connection)
        create_assets_table(connection)

        # These tables remain in the database even when they are not
        # included as input sheets in the Excel workbook.
        create_holdings_summary_table(connection)
        create_portfolio_daily_table(connection)
        migrate_portfolio_daily_table(connection)
        create_security_daily_table(connection)
        create_market_prices_table(connection)


        create_indexes(connection)

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    validate_database()




def main() -> None:
    """
    Create and validate the portfolio database.
    """

    create_database()

    print(f"Database created successfully: {DATABASE_PATH}")
    print("Database structure validated successfully.")


if __name__ == "__main__":
    main()
