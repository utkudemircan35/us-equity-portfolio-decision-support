from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any

import pandas as pd

import database


VALUE_TOLERANCE = 1e-6
SHARE_TOLERANCE = 1e-8
WEIGHT_TOLERANCE = 1e-6


@dataclass
class PortfolioValidationResult:
    is_valid: bool
    summary: dict[str, Any]
    checks: pd.DataFrame


def build_check(
    check_key: str,
    check_name: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build one validation result record.
    """

    return {
        "check_key": check_key,
        "check_name": check_name,
        "status": status,
        "message": message,
        "details": details or {},
    }


def values_are_finite(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> bool:
    """
    Check whether selected values are finite numbers.
    """

    for row in rows:
        for column in columns:
            value = row.get(column)

            if value is None:
                return False

            try:
                numeric_value = float(value)

            except (TypeError, ValueError):
                return False

            if not math.isfinite(numeric_value):
                return False

    return True


def run_portfolio_validation(
) -> PortfolioValidationResult:
    """
    Run structural and financial consistency checks.
    """

    checks: list[dict[str, Any]] = []

    try:
        database.validate_database()

    except Exception as error:
        checks.append(
            build_check(
                check_key="database_schema",
                check_name="Database schema",
                status="FAIL",
                message=str(error),
            )
        )

        return PortfolioValidationResult(
            is_valid=False,
            summary={
                "status": "FAIL",
                "passed_checks": 0,
                "warning_checks": 0,
                "failed_checks": 1,
                "total_checks": 1,
                "checked_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            },
            checks=pd.DataFrame(checks),
        )

    checks.append(
        build_check(
            check_key="database_schema",
            check_name="Database schema",
            status="PASS",
            message=(
                "All required tables and "
                "columns are available."
            ),
        )
    )

    raw_counts = database.fetch_one(
        """
        SELECT
            (
                SELECT COUNT(*)
                FROM trades
            ) AS trades,

            (
                SELECT COUNT(*)
                FROM cash_movements
            ) AS cash_movements,

            (
                SELECT COUNT(*)
                FROM dividends
            ) AS dividends
        """
    )

    trade_count = int(
        raw_counts["trades"]
    )

    cash_count = int(
        raw_counts["cash_movements"]
    )

    dividend_count = int(
        raw_counts["dividends"]
    )

    if trade_count == 0:
        checks.append(
            build_check(
                check_key="source_data",
                check_name="Source data",
                status="FAIL",
                message="No trades are available.",
                details=raw_counts,
            )
        )

    else:
        status = (
            "PASS"
            if cash_count > 0
            else "WARNING"
        )

        message = (
            "Transaction source tables "
            "contain data."
            if cash_count > 0
            else (
                "Trades exist, but no cash "
                "movements are available."
            )
        )

        checks.append(
            build_check(
                check_key="source_data",
                check_name="Source data",
                status=status,
                message=message,
                details={
                    "trades": trade_count,
                    "cash_movements": cash_count,
                    "dividends": dividend_count,
                },
            )
        )

    invalid_trade_count = database.fetch_one(
        """
        SELECT
            COUNT(*) AS invalid_count
        FROM trades
        WHERE cumulative_shares < ?
           OR shares <= 0
           OR raw_shares <= 0
           OR split_factor <= 0
           OR commission_usd < 0
           OR (
               is_gift = 1
               AND side <> 'Buy'
           )
           OR (
               is_gift = 1
               AND ABS(net_flow_usd) > ?
           )
        """,
        (
            -SHARE_TOLERANCE,
            VALUE_TOLERANCE,
        ),
    )["invalid_count"]

    checks.append(
        build_check(
            check_key="trade_integrity",
            check_name="Trade integrity",
            status=(
                "PASS"
                if invalid_trade_count == 0
                else "FAIL"
            ),
            message=(
                "Trade balances and business "
                "rules are valid."
                if invalid_trade_count == 0
                else (
                    f"{invalid_trade_count} "
                    "invalid trade rows were found."
                )
            ),
            details={
                "invalid_rows": int(
                    invalid_trade_count
                ),
            },
        )
    )

    holding_differences = database.fetch_all(
        """
        WITH ranked_trades AS (
            SELECT
                ticker,
                cumulative_shares,

                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY
                        date DESC,
                        COALESCE(
                            time,
                            '00:00:00'
                        ) DESC,
                        trade_id DESC
                ) AS row_number

            FROM trades
        )

        SELECT
            t.ticker,
            t.cumulative_shares
                AS trade_shares,
            h.shares
                AS holding_shares

        FROM ranked_trades AS t

        LEFT JOIN holdings_summary AS h
          ON h.ticker = t.ticker

        WHERE t.row_number = 1
          AND (
              h.ticker IS NULL
              OR ABS(
                  t.cumulative_shares
                  - h.shares
              ) > ?
          )

        ORDER BY t.ticker
        """,
        (SHARE_TOLERANCE,),
    )

    checks.append(
        build_check(
            check_key=(
                "holdings_reconciliation"
            ),
            check_name=(
                "Holdings reconciliation"
            ),
            status=(
                "PASS"
                if not holding_differences
                else "FAIL"
            ),
            message=(
                "Final trade balances match "
                "holdings_summary."
                if not holding_differences
                else (
                    "Trade balances do not match "
                    "holdings_summary."
                )
            ),
            details={
                "differences":
                    holding_differences,
            },
        )
    )

    cash_values = database.fetch_one(
        """
        SELECT
            COALESCE(
                (
                    SELECT SUM(net_flow_usd)
                    FROM trades
                ),
                0
            )
            +
            COALESCE(
                (
                    SELECT SUM(
                        CASE
                            WHEN type = 'WITHDRAWAL'
                            THEN -amount_usd
                            ELSE amount_usd
                        END
                    )
                    FROM cash_movements
                ),
                0
            )
            +
            COALESCE(
                (
                    SELECT SUM(
                        net_dividend_usd
                    )
                    FROM dividends
                ),
                0
            ) AS expected_cash_usd,

            COALESCE(
                (
                    SELECT market_value_usd
                    FROM holdings_summary
                    WHERE ticker = 'CASH'
                ),
                0
            ) AS actual_cash_usd
        """
    )

    cash_difference = (
        float(
            cash_values[
                "expected_cash_usd"
            ]
        )
        - float(
            cash_values[
                "actual_cash_usd"
            ]
        )
    )

    cash_matches = (
        abs(cash_difference)
        <= VALUE_TOLERANCE
    )

    checks.append(
        build_check(
            check_key="cash_reconciliation",
            check_name="Cash reconciliation",
            status=(
                "PASS"
                if cash_matches
                else "FAIL"
            ),
            message=(
                "Calculated cash matches "
                "the CASH holding."
                if cash_matches
                else (
                    "Calculated cash does not "
                    "match the CASH holding."
                )
            ),
            details={
                "expected_cash_usd": float(
                    cash_values[
                        "expected_cash_usd"
                    ]
                ),
                "actual_cash_usd": float(
                    cash_values[
                        "actual_cash_usd"
                    ]
                ),
                "difference_usd":
                    cash_difference,
            },
        )
    )

    portfolio_values = database.fetch_one(
        """
        SELECT
            COALESCE(
                (
                    SELECT SUM(
                        market_value_usd
                    )
                    FROM holdings_summary
                ),
                0
            ) AS holdings_value_usd,

            COALESCE(
                (
                    SELECT closing_value_usd
                    FROM portfolio_daily
                    ORDER BY date DESC
                    LIMIT 1
                ),
                0
            ) AS daily_value_usd
        """
    )

    portfolio_difference = (
        float(
            portfolio_values[
                "holdings_value_usd"
            ]
        )
        - float(
            portfolio_values[
                "daily_value_usd"
            ]
        )
    )

    portfolio_matches = (
        abs(portfolio_difference)
        <= VALUE_TOLERANCE
    )

    checks.append(
        build_check(
            check_key=(
                "portfolio_reconciliation"
            ),
            check_name=(
                "Portfolio reconciliation"
            ),
            status=(
                "PASS"
                if portfolio_matches
                else "FAIL"
            ),
            message=(
                "portfolio_daily matches "
                "holdings_summary."
                if portfolio_matches
                else (
                    "portfolio_daily does not "
                    "match holdings_summary."
                )
            ),
            details={
                "holdings_value_usd": float(
                    portfolio_values[
                        "holdings_value_usd"
                    ]
                ),
                "daily_value_usd": float(
                    portfolio_values[
                        "daily_value_usd"
                    ]
                ),
                "difference_usd":
                    portfolio_difference,
            },
        )
    )

    weight_values = database.fetch_one(
        """
        SELECT
            COALESCE(
                SUM(weight),
                0
            ) AS total_weight,

            COALESCE(
                SUM(market_value_usd),
                0
            ) AS portfolio_value_usd

        FROM holdings_summary
        WHERE status = 'OPEN'
        """
    )

    total_weight = float(
        weight_values["total_weight"]
    )

    portfolio_value = float(
        weight_values[
            "portfolio_value_usd"
        ]
    )

    if (
        portfolio_value
        > VALUE_TOLERANCE
    ):
        weight_matches = (
            abs(total_weight - 1.0)
            <= WEIGHT_TOLERANCE
        )

    else:
        weight_matches = (
            abs(total_weight)
            <= WEIGHT_TOLERANCE
        )

    checks.append(
        build_check(
            check_key="portfolio_weights",
            check_name="Portfolio weights",
            status=(
                "PASS"
                if weight_matches
                else "FAIL"
            ),
            message=(
                "Open-position weights have "
                "a valid total."
                if weight_matches
                else (
                    "Open-position weights do "
                    "not have a valid total."
                )
            ),
            details={
                "total_weight": total_weight,
                "portfolio_value_usd":
                    portfolio_value,
            },
        )
    )

    missing_prices = database.fetch_all(
        """
        SELECT
            h.ticker,
            h.price_usd,
            COUNT(p.key)
                AS price_observations

        FROM holdings_summary AS h

        LEFT JOIN market_prices AS p
          ON p.ticker = h.ticker

        WHERE h.status = 'OPEN'
          AND h.ticker <> 'CASH'

        GROUP BY
            h.ticker,
            h.price_usd

        HAVING h.price_usd <= 0
            OR COUNT(p.key) = 0

        ORDER BY h.ticker
        """
    )

    checks.append(
        build_check(
            check_key="open_market_prices",
            check_name=(
                "Open-position market prices"
            ),
            status=(
                "PASS"
                if not missing_prices
                else "FAIL"
            ),
            message=(
                "Every open security has "
                "market-price history."
                if not missing_prices
                else (
                    "Some open securities do "
                    "not have usable prices."
                )
            ),
            details={
                "missing_prices":
                    missing_prices,
            },
        )
    )

    security_differences = (
        database.fetch_all(
            """
            WITH latest_security AS (
                SELECT
                    ticker,
                    closing_shares,
                    closing_value_usd,

                    ROW_NUMBER() OVER (
                        PARTITION BY ticker
                        ORDER BY date DESC
                    ) AS row_number

                FROM security_daily
            )

            SELECT
                h.ticker,
                h.shares
                    AS holding_shares,
                h.market_value_usd
                    AS holding_value_usd,
                s.closing_shares,
                s.closing_value_usd

            FROM holdings_summary AS h

            LEFT JOIN latest_security AS s
              ON s.ticker = h.ticker
             AND s.row_number = 1

            WHERE h.status = 'OPEN'
              AND h.ticker <> 'CASH'
              AND (
                  s.ticker IS NULL
                  OR ABS(
                      h.shares
                      - s.closing_shares
                  ) > ?
                  OR ABS(
                      h.market_value_usd
                      - s.closing_value_usd
                  ) > ?
              )

            ORDER BY h.ticker
            """,
            (
                SHARE_TOLERANCE,
                VALUE_TOLERANCE,
            ),
        )
    )

    checks.append(
        build_check(
            check_key=(
                "security_daily_reconciliation"
            ),
            check_name=(
                "Security daily reconciliation"
            ),
            status=(
                "PASS"
                if not security_differences
                else "FAIL"
            ),
            message=(
                "security_daily matches all "
                "open holdings."
                if not security_differences
                else (
                    "security_daily does not "
                    "match all open holdings."
                )
            ),
            details={
                "differences":
                    security_differences,
            },
        )
    )

    missing_assets = database.fetch_all(
        """
        SELECT
            h.ticker

        FROM holdings_summary AS h

        LEFT JOIN assets AS a
          ON a.ticker = h.ticker

        WHERE a.ticker IS NULL

        ORDER BY h.ticker
        """
    )

    checks.append(
        build_check(
            check_key="asset_metadata",
            check_name="Asset metadata",
            status=(
                "PASS"
                if not missing_assets
                else "WARNING"
            ),
            message=(
                "Asset metadata is available "
                "for all holdings."
                if not missing_assets
                else (
                    "Some holdings do not have "
                    "asset metadata."
                )
            ),
            details={
                "missing_assets":
                    missing_assets,
            },
        )
    )

    numeric_rows = database.fetch_all(
        """
        SELECT
            shares,
            price_usd,
            market_value_usd,
            weight,
            remaining_cost_basis_usd,
            average_cost_usd,
            realized_pnl_usd,
            unrealized_pnl_usd

        FROM holdings_summary
        """
    )

    numeric_columns = [
        "shares",
        "price_usd",
        "market_value_usd",
        "weight",
        "remaining_cost_basis_usd",
        "average_cost_usd",
        "realized_pnl_usd",
        "unrealized_pnl_usd",
    ]

    finite_values = values_are_finite(
        rows=numeric_rows,
        columns=numeric_columns,
    )

    checks.append(
        build_check(
            check_key="numeric_values",
            check_name="Numeric values",
            status=(
                "PASS"
                if finite_values
                else "FAIL"
            ),
            message=(
                "Calculated holding values "
                "are finite."
                if finite_values
                else (
                    "Invalid numeric holding "
                    "values were found."
                )
            ),
        )
    )

    checks_table = pd.DataFrame(
        checks
    )

    passed_checks = int(
        (
            checks_table["status"]
            == "PASS"
        ).sum()
    )

    warning_checks = int(
        (
            checks_table["status"]
            == "WARNING"
        ).sum()
    )

    failed_checks = int(
        (
            checks_table["status"]
            == "FAIL"
        ).sum()
    )

    is_valid = (
        failed_checks == 0
    )

    if (
        is_valid
        and warning_checks == 0
    ):
        overall_status = "PASS"

    elif is_valid:
        overall_status = "WARNING"

    else:
        overall_status = "FAIL"

    summary = {
        "status": overall_status,
        "passed_checks": passed_checks,
        "warning_checks": warning_checks,
        "failed_checks": failed_checks,
        "total_checks": int(
            len(checks_table)
        ),
        "checked_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    return PortfolioValidationResult(
        is_valid=is_valid,
        summary=summary,
        checks=checks_table,
    )


def main() -> None:
    """
    Run and print the portfolio validation report.
    """

    result = (
        run_portfolio_validation()
    )

    print(
        "Portfolio validation completed."
    )

    print()
    print(result.summary)
    print()

    with pd.option_context(
        "display.max_columns",
        None,
        "display.width",
        180,
    ):
        print(
            result.checks[
                [
                    "check_key",
                    "status",
                    "message",
                ]
            ].to_string(
                index=False
            )
        )

    if not result.is_valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
