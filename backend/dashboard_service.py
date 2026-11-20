from typing import Any, Literal

import pandas as pd

from database import fetch_all, fetch_one, validate_database
from performance_calculator import build_security_performance_table
from portfolio_optimizer import build_portfolio_optimizer
from rebalancing_calculator import build_rebalancing_plan
from risk_calculator import build_risk_analysis, resolve_risk_free_rate
from portfolio_validation_service import (
    run_portfolio_validation,
)

HoldingStatus = Literal["OPEN", "CLOSED", "ALL"]
AllocationGroup = Literal[
    "ticker",
    "asset_type",
    "sector",
    "industry",
]


def get_portfolio_health(
) -> dict[str, Any]:
    """
    Return the latest portfolio validation report.
    """

    result = (
        run_portfolio_validation()
    )

    return {
        "is_valid": result.is_valid,
        "summary": (
            result.summary.copy()
        ),
        "checks": (
            result.checks.copy()
        ),
    }

def rows_to_dataframe(rows: list[Any]) -> pd.DataFrame:
    """Convert database rows to a DataFrame."""

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(
        [
            dict(row) if hasattr(row, "keys") else row
            for row in rows
        ]
    )


def normalize_date_filter(
    value: str | None,
    field_name: str,
) -> str | None:
    """Validate an optional date filter and return YYYY-MM-DD."""

    if value is None:
        return None

    parsed_value = pd.to_datetime(
        value,
        errors="coerce",
    )

    if pd.isna(parsed_value):
        raise ValueError(
            f"{field_name} must use a valid date value."
        )

    return parsed_value.strftime("%Y-%m-%d")


def get_portfolio_overview() -> dict[str, Any]:
    """Return the main portfolio-level dashboard metrics."""

    validate_database()

    holdings = fetch_one(
        """
        SELECT
            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'OPEN'
                         AND ticker <> 'CASH'
                        THEN market_value_usd
                        ELSE 0
                    END
                ),
                0
            ) AS securities_value_usd,
            COALESCE(
                SUM(
                    CASE
                        WHEN ticker = 'CASH'
                        THEN market_value_usd
                        ELSE 0
                    END
                ),
                0
            ) AS cash_balance_usd,
            COALESCE(
                SUM(
                    CASE
                        WHEN ticker <> 'CASH'
                        THEN realized_pnl_usd
                        ELSE 0
                    END
                ),
                0
            ) AS realized_pnl_usd,
            COALESCE(
                SUM(
                    CASE
                        WHEN ticker <> 'CASH'
                        THEN unrealized_pnl_usd
                        ELSE 0
                    END
                ),
                0
            ) AS unrealized_pnl_usd,
            SUM(
                CASE
                    WHEN status = 'OPEN'
                     AND ticker <> 'CASH'
                    THEN 1
                    ELSE 0
                END
            ) AS open_position_count,
            SUM(
                CASE
                    WHEN status = 'CLOSED'
                     AND ticker <> 'CASH'
                    THEN 1
                    ELSE 0
                END
            ) AS closed_position_count,
            MAX(as_of_date) AS holdings_as_of_date
        FROM holdings_summary
        """
    )

    if holdings is None:
        raise RuntimeError(
            "The holdings summary is empty. Run the portfolio pipeline first."
        )

    latest_daily = fetch_one(
        """
        SELECT
            date,
            opening_value_usd,
            securities_value_usd,
            cash_balance_usd,
            closing_value_usd,
            daily_return,
            cumulative_twr
        FROM portfolio_daily
        ORDER BY date DESC
        LIMIT 1
        """
    )

    if latest_daily is None:
        raise RuntimeError(
            "The daily portfolio table is empty. Run the portfolio pipeline first."
        )

    external_capital = fetch_one(
        """
        SELECT
            COALESCE(
                SUM(
                    CASE
                        WHEN type = 'DEPOSIT'
                        THEN amount_usd
                        WHEN type = 'WITHDRAWAL'
                        THEN -amount_usd
                        ELSE 0
                    END
                ),
                0
            ) AS net_external_capital_usd,
            COALESCE(
                SUM(
                    CASE
                        WHEN type = 'INTEREST'
                        THEN amount_usd
                        ELSE 0
                    END
                ),
                0
            ) AS total_interest_usd
        FROM cash_movements
        """
    )

    dividend_total = fetch_one(
        """
        SELECT
            COALESCE(
                SUM(net_dividend_usd),
                0
            ) AS total_dividend_usd
        FROM dividends
        """
    )

    securities_value = float(
        holdings["securities_value_usd"]
    )
    cash_balance = float(
        holdings["cash_balance_usd"]
    )
    portfolio_value = securities_value + cash_balance

    cash_weight = (
        cash_balance / portfolio_value
        if portfolio_value != 0
        else 0.0
    )

    realized_pnl = float(
        holdings["realized_pnl_usd"]
    )
    unrealized_pnl = float(
        holdings["unrealized_pnl_usd"]
    )

    daily_closing_value = float(
        latest_daily["closing_value_usd"]
    )

    return {
        "valuation_date": latest_daily["date"],
        "holdings_as_of_date": holdings["holdings_as_of_date"],
        "portfolio_value_usd": portfolio_value,
        "securities_value_usd": securities_value,
        "cash_balance_usd": cash_balance,
        "cash_weight": cash_weight,
        "realized_pnl_usd": realized_pnl,
        "unrealized_pnl_usd": unrealized_pnl,
        "total_pnl_usd": realized_pnl + unrealized_pnl,
        "open_position_count": int(
            holdings["open_position_count"] or 0
        ),
        "closed_position_count": int(
            holdings["closed_position_count"] or 0
        ),
        "daily_return": float(
            latest_daily["daily_return"]
        ),
        "cumulative_twr": float(
            latest_daily["cumulative_twr"]
        ),
        "net_external_capital_usd": float(
            external_capital["net_external_capital_usd"]
        ),
        "total_interest_usd": float(
            external_capital["total_interest_usd"]
        ),
        "total_dividend_usd": float(
            dividend_total["total_dividend_usd"]
        ),
        "reconciliation_difference_usd": (
            portfolio_value - daily_closing_value
        ),
    }


def get_holdings_table(
    status: HoldingStatus = "OPEN",
    include_cash: bool = True,
) -> pd.DataFrame:
    """Return holdings with asset metadata without rounding values."""

    validate_database()

    normalized_status = str(status).strip().upper()

    if normalized_status not in {"OPEN", "CLOSED", "ALL"}:
        raise ValueError(
            "status must be OPEN, CLOSED or ALL."
        )

    conditions: list[str] = []
    parameters: list[Any] = []

    if normalized_status != "ALL":
        conditions.append("h.status = ?")
        parameters.append(normalized_status)

    if not include_cash:
        conditions.append("h.ticker <> 'CASH'")

    where_clause = (
        "WHERE " + " AND ".join(conditions)
        if conditions
        else ""
    )

    rows = fetch_all(
        f"""
        SELECT
            h.ticker,
            COALESCE(a.company_name, h.ticker) AS company_name,
            COALESCE(a.asset_type, 'UNKNOWN') AS asset_type,
            COALESCE(a.sector, 'Unknown') AS sector,
            COALESCE(a.industry, 'Unknown') AS industry,
            h.as_of_date,
            h.status,
            h.shares,
            h.price_usd,
            h.market_value_usd,
            h.weight,
            h.remaining_cost_basis_usd,
            h.average_cost_usd,
            h.realized_pnl_usd,
            h.unrealized_pnl_usd,
            (
                h.realized_pnl_usd
                + h.unrealized_pnl_usd
            ) AS total_pnl_usd
        FROM holdings_summary AS h
        LEFT JOIN assets AS a
          ON a.ticker = h.ticker
        {where_clause}
        ORDER BY
            CASE WHEN h.ticker = 'CASH' THEN 0 ELSE 1 END,
            h.market_value_usd DESC,
            h.ticker
        """,
        tuple(parameters),
    )

    return rows_to_dataframe(rows)


def get_allocation_summary(
    group_by: AllocationGroup = "ticker",
) -> pd.DataFrame:
    """Return current allocation grouped by a supported asset field."""

    validate_database()

    allowed_columns = {
        "ticker": "h.ticker",
        "asset_type": "COALESCE(a.asset_type, 'UNKNOWN')",
        "sector": "COALESCE(a.sector, 'Unknown')",
        "industry": "COALESCE(a.industry, 'Unknown')",
    }

    if group_by not in allowed_columns:
        raise ValueError(
            "group_by must be ticker, asset_type, sector or industry."
        )

    group_expression = allowed_columns[group_by]

    rows = fetch_all(
        f"""
        SELECT
            {group_expression} AS category,
            SUM(h.market_value_usd) AS market_value_usd,
            SUM(h.weight) AS weight
        FROM holdings_summary AS h
        LEFT JOIN assets AS a
          ON a.ticker = h.ticker
        WHERE h.status = 'OPEN'
          AND h.market_value_usd > 0
        GROUP BY {group_expression}
        ORDER BY market_value_usd DESC, category
        """
    )

    allocation = rows_to_dataframe(rows)

    if not allocation.empty:
        allocation.insert(
            0,
            "group_by",
            group_by,
        )

    return allocation


def get_portfolio_history(
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Return daily portfolio history for an optional date range."""

    validate_database()

    normalized_start = normalize_date_filter(
        start_date,
        "start_date",
    )
    normalized_end = normalize_date_filter(
        end_date,
        "end_date",
    )

    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_start > normalized_end
    ):
        raise ValueError(
            "start_date cannot be after end_date."
        )

    conditions: list[str] = []
    parameters: list[Any] = []

    if normalized_start is not None:
        conditions.append("date >= ?")
        parameters.append(normalized_start)

    if normalized_end is not None:
        conditions.append("date <= ?")
        parameters.append(normalized_end)

    where_clause = (
        "WHERE " + " AND ".join(conditions)
        if conditions
        else ""
    )

    rows = fetch_all(
        f"""
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
        {where_clause}
        ORDER BY date
        """,
        tuple(parameters),
    )

    history = rows_to_dataframe(rows)

    if not history.empty:
        history["date"] = pd.to_datetime(
            history["date"],
            errors="raise",
        )

    return history


def get_recent_activity(
    limit: int = 20,
) -> pd.DataFrame:
    """Return recent trades, cash movements and dividends together."""

    validate_database()

    if limit <= 0:
        raise ValueError("limit must be greater than zero.")

    rows = fetch_all(
        """
        SELECT
            activity_id,
            date,
            time,
            category,
            event_type,
            ticker,
            quantity,
            price_usd,
            commission_usd,
            cash_flow_usd
        FROM (
            SELECT
                'TRADE-' || trade_id AS activity_id,
                date,
                time,
                'TRADE' AS category,
                UPPER(side) AS event_type,
                ticker,
                shares AS quantity,
                price_usd,
                commission_usd,
                net_flow_usd AS cash_flow_usd,
                COALESCE(time, '00:00:00') AS sort_time
            FROM trades

            UNION ALL

            SELECT
                'CASH-' || cash_id AS activity_id,
                date,
                time,
                'CASH' AS category,
                type AS event_type,
                'CASH' AS ticker,
                NULL AS quantity,
                NULL AS price_usd,
                NULL AS commission_usd,
                CASE
                    WHEN type = 'WITHDRAWAL'
                    THEN -amount_usd
                    ELSE amount_usd
                END AS cash_flow_usd,
                COALESCE(time, '00:00:00') AS sort_time
            FROM cash_movements

            UNION ALL

            SELECT
                'DIVIDEND-' || dividend_id AS activity_id,
                date,
                time,
                'DIVIDEND' AS category,
                'DIVIDEND' AS event_type,
                ticker,
                NULL AS quantity,
                NULL AS price_usd,
                NULL AS commission_usd,
                net_dividend_usd AS cash_flow_usd,
                COALESCE(time, '00:00:00') AS sort_time
            FROM dividends
        ) AS activity
        ORDER BY
            date DESC,
            sort_time DESC,
            activity_id DESC
        LIMIT ?
        """,
        (limit,),
    )

    activity = rows_to_dataframe(rows)

    if not activity.empty:
        activity["date"] = pd.to_datetime(
            activity["date"],
            errors="raise",
        )

    return activity

def get_available_security_tickers() -> pd.DataFrame:
    """
    Return securities that have daily history.
    """

    validate_database()

    rows = fetch_all(
        """
        SELECT
            s.ticker,
            COALESCE(
                a.company_name,
                s.ticker
            ) AS company_name,
            COALESCE(
                a.asset_type,
                'UNKNOWN'
            ) AS asset_type,
            COALESCE(
                a.sector,
                'Unknown'
            ) AS sector,
            COALESCE(
                a.industry,
                'Unknown'
            ) AS industry,
            h.status,
            MIN(s.date) AS start_date,
            MAX(s.date) AS end_date,
            COUNT(*) AS observations

        FROM security_daily AS s

        LEFT JOIN assets AS a
          ON a.ticker = s.ticker

        LEFT JOIN holdings_summary AS h
          ON h.ticker = s.ticker

        GROUP BY
            s.ticker,
            a.company_name,
            a.asset_type,
            a.sector,
            a.industry,
            h.status

        ORDER BY
            CASE h.status
                WHEN 'OPEN' THEN 0
                WHEN 'CLOSED' THEN 1
                ELSE 2
            END,
            s.ticker
        """
    )

    tickers = rows_to_dataframe(rows)

    if tickers.empty:
        return tickers

    tickers["start_date"] = pd.to_datetime(
        tickers["start_date"],
        errors="raise",
    )

    tickers["end_date"] = pd.to_datetime(
        tickers["end_date"],
        errors="raise",
    )

    tickers["observations"] = pd.to_numeric(
        tickers["observations"],
        errors="raise",
    ).astype(int)

    return tickers

def get_security_history(
    ticker: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """
    Return daily valuation and TWR history
    for one security.
    """

    validate_database()

    normalized_ticker = (
        str(ticker)
        .strip()
        .upper()
    )

    if not normalized_ticker:
        raise ValueError(
            "ticker cannot be empty."
        )

    normalized_start = normalize_date_filter(
        start_date,
        "start_date",
    )

    normalized_end = normalize_date_filter(
        end_date,
        "end_date",
    )

    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_start
        > normalized_end
    ):
        raise ValueError(
            "start_date cannot be after end_date."
        )

    conditions = [
        "ticker = ?",
    ]

    parameters: list[Any] = [
        normalized_ticker,
    ]

    if normalized_start is not None:
        conditions.append(
            "date >= ?"
        )

        parameters.append(
            normalized_start
        )

    if normalized_end is not None:
        conditions.append(
            "date <= ?"
        )

        parameters.append(
            normalized_end
        )

    where_clause = (
        " AND ".join(conditions)
    )

    rows = fetch_all(
        f"""
        SELECT
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

        FROM security_daily

        WHERE {where_clause}

        ORDER BY date
        """,
        tuple(parameters),
    )

    history = rows_to_dataframe(rows)

    if history.empty:
        raise ValueError(
            "No security history was found for "
            f"{normalized_ticker}."
        )

    history["date"] = pd.to_datetime(
        history["date"],
        errors="raise",
    )

    numeric_columns = [
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

    for column in numeric_columns:
        history[column] = pd.to_numeric(
            history[column],
            errors="raise",
        )

    return history

def get_security_performance(
    risk_free_rate: float | None = None,
) -> dict[str, Any]:
    """Return security performance with the resolved risk-free rate."""

    validate_database()

    risk_free_details = resolve_risk_free_rate(
        risk_free_rate
    )

    performance = build_security_performance_table(
        risk_free_rate=float(
            risk_free_details["rate"]
        )
    )

    return {
        "risk_free_rate": risk_free_details,
        "performance": performance,
    }


def get_risk_dashboard_data(
    start_date: str | None = None,
    end_date: str | None = None,
    risk_free_rate: float | None = None,
) -> dict[str, Any]:
    """Return all risk-analysis outputs for the future frontend."""

    result = build_risk_analysis(
        start_date=start_date,
        end_date=end_date,
        risk_free_rate=risk_free_rate,
    )

    return {
        "summary": result.summary,
        "security_metrics": result.security_metrics,
        "correlation_matrix": result.correlation_matrix,
        "covariance_matrix": result.covariance_matrix,
        "risk_contribution": result.risk_contribution,
    }


def get_optimization_dashboard_data(
    lookback_years: int = 3,
    minimum_observations: int = 252,
    min_weight: float = 0.0,
    max_weight: float = 0.20,
    cash_reserve: float | None = None,
    frontier_points: int = 40,
    risk_free_rate: float | None = None,
    selected_tickers: list[str] | None = None,
) -> dict[str, Any]:
    """Return optimization outputs without printing them."""

    result = build_portfolio_optimizer(
        lookback_years=lookback_years,
        minimum_observations=minimum_observations,
        min_weight=min_weight,
        max_weight=max_weight,
        cash_reserve=cash_reserve,
        frontier_points=frontier_points,
        risk_free_rate=risk_free_rate,
        selected_tickers=selected_tickers,
    )

    return {
        "summary": result.summary,
        "allocation": result.allocation,
        "strategy_metrics": result.strategy_metrics,
        "efficient_frontier": result.efficient_frontier,
        "efficient_frontier_weights": (
            result.efficient_frontier_weights
        ),
        "expected_returns": result.expected_returns,
        "covariance_matrix": result.covariance_matrix,
    }


def get_rebalancing_dashboard_data(
    strategy: str = "maximum_sharpe",
    frontier_point: int | None = None,
    commission_per_trade: float = 0.0,
    minimum_trade_amount: float = 5.0,
    fractional_shares: bool = True,
    selected_tickers: list[str] | None = None,
    lookback_years: int = 3,
    minimum_observations: int = 252,
    min_weight: float = 0.0,
    max_weight: float = 0.20,
    cash_reserve: float | None = None,
    frontier_points: int = 40,
    risk_free_rate: float | None = None,
) -> dict[str, Any]:
    """Return rebalancing recommendations and their optimization data."""

    result = build_rebalancing_plan(
        strategy=strategy,
        frontier_point=frontier_point,
        commission_per_trade=commission_per_trade,
        minimum_trade_amount=minimum_trade_amount,
        fractional_shares=fractional_shares,
        selected_tickers=selected_tickers,
        lookback_years=lookback_years,
        minimum_observations=minimum_observations,
        min_weight=min_weight,
        max_weight=max_weight,
        cash_reserve=cash_reserve,
        frontier_points=frontier_points,
        risk_free_rate=risk_free_rate,
    )

    return {
        "summary": result.summary,
        "recommendations": result.recommendations,
        "optimization": {
            "summary": result.optimization.summary,
            "allocation": result.optimization.allocation,
            "strategy_metrics": (
                result.optimization.strategy_metrics
            ),
            "efficient_frontier": (
                result.optimization.efficient_frontier
            ),
            "efficient_frontier_weights": (
                result.optimization.efficient_frontier_weights
            ),
        },
    }


def get_overview_dashboard_data(
    recent_activity_limit: int = 20,
) -> dict[str, Any]:
    """Return the lightweight data needed by Portfolio Overview."""

    return {
        "overview": get_portfolio_overview(),
        "holdings": get_holdings_table(
            status="OPEN",
            include_cash=True,
        ),
        "ticker_allocation": get_allocation_summary(
            group_by="ticker"
        ),
        "sector_allocation": get_allocation_summary(
            group_by="sector"
        ),
        "asset_type_allocation": get_allocation_summary(
            group_by="asset_type"
        ),
        "portfolio_history": get_portfolio_history(),
        "recent_activity": get_recent_activity(
            limit=recent_activity_limit
        ),
    }


def main() -> None:
    """Run a lightweight dashboard-service smoke test."""

    dashboard = get_overview_dashboard_data()

    print("Dashboard service loaded successfully.")
    print()
    print("PORTFOLIO OVERVIEW")

    for key, value in dashboard["overview"].items():
        print(f"{key}: {value}")

    print()
    print(f"Holdings rows: {len(dashboard['holdings'])}")
    print(
        "Portfolio history rows: "
        f"{len(dashboard['portfolio_history'])}"
    )
    print(
        "Recent activity rows: "
        f"{len(dashboard['recent_activity'])}"
    )


if __name__ == "__main__":
    main()
