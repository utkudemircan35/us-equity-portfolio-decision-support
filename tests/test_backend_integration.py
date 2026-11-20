import numpy as np
import pandas as pd
import pytest

import database
import performance_calculator
import portfolio_optimizer
import portfolio_refresh_service
import rebalancing_calculator
import risk_calculator
import transaction_service
import portfolio_validation_service

def install_fake_asset_refresh(
    monkeypatch,
):
    """
    Replace yfinance asset metadata with
    deterministic test records.
    """

    def fake_refresh_asset_metadata():
        rows = [
            (
                "AAPL",
                "Apple Inc.",
                "EQUITY",
                "Technology",
                "Consumer Electronics",
            ),
            (
                "MSFT",
                "Microsoft Corporation",
                "EQUITY",
                "Technology",
                "Software - Infrastructure",
            ),
            (
                "SPY",
                "SPDR S&P 500 ETF Trust",
                "ETF",
                "ETF / Fund",
                "Exchange Traded Fund",
            ),
            (
                "CASH",
                "US Dollar Cash",
                "CASH",
                "Cash",
                "Cash",
            ),
        ]

        with database.database_transaction() as connection:
            connection.execute(
                "DELETE FROM assets"
            )

            connection.executemany(
                """
                INSERT INTO assets (
                    ticker,
                    company_name,
                    asset_type,
                    sector,
                    industry
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

        return {
            "saved_rows": len(rows),
            "equities": 2,
            "etfs": 1,
            "cash": 1,
            "unknown": 0,
            "unavailable_tickers": [],
            "failed_downloads": {},
        }

    monkeypatch.setattr(
        portfolio_refresh_service,
        "refresh_asset_metadata",
        fake_refresh_asset_metadata,
    )


def install_fake_market_refresh(
    monkeypatch,
):
    """
    Replace yfinance market downloads with deterministic
    daily prices for AAPL, MSFT and SPY.
    """

    def fake_refresh_market_data_safely():
        dates = pd.bdate_range(
            start="2024-01-02",
            periods=90,
        )

        price_configuration = {
            "AAPL": {
                "starting_price": 100.0,
                "daily_growth": 1.0010,
            },
            "MSFT": {
                "starting_price": 200.0,
                "daily_growth": 1.0008,
            },
            "SPY": {
                "starting_price": 400.0,
                "daily_growth": 1.0006,
            },
        }

        rows = []

        for (
            ticker,
            configuration,
        ) in price_configuration.items():
            starting_price = float(
                configuration[
                    "starting_price"
                ]
            )

            daily_growth = float(
                configuration[
                    "daily_growth"
                ]
            )

            for observation, date in enumerate(
                dates
            ):
                close_usd = (
                    starting_price
                    * (
                        daily_growth
                        ** observation
                    )
                )

                date_text = (
                    date.strftime(
                        "%Y-%m-%d"
                    )
                )

                rows.append(
                    (
                        (
                            f"{ticker}|"
                            f"{date_text}"
                        ),
                        ticker,
                        date_text,
                        float(close_usd),
                        float(close_usd),
                    )
                )

        with database.database_transaction() as connection:
            connection.execute(
                "DELETE FROM market_prices"
            )

            connection.executemany(
                """
                INSERT INTO market_prices (
                    key,
                    ticker,
                    date,
                    close_usd,
                    adjusted_close_usd
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

        return {
            "saved_rows": len(rows),
            "open_tickers": [
                "AAPL",
                "MSFT",
            ],
            "closed_tickers": [],
            "benchmark_tickers": [
                "SPY",
            ],
            "trade_fallback_tickers": {},
            "failed_required_tickers": {},
        }

    monkeypatch.setattr(
        portfolio_refresh_service,
        "refresh_market_data_safely",
        fake_refresh_market_data_safely,
    )


def seed_portfolio_transactions():
    """
    Add a deterministic portfolio without refreshing
    after each source transaction.
    """

    transaction_service.add_cash_movement(
        date="2024-01-02",
        time_value="09:00:00",
        movement_type="DEPOSIT",
        amount_usd=1000.0,
        refresh_calculated_data=False,
    )

    transaction_service.add_trade(
        date="2024-01-02",
        time_value="10:00:00",
        ticker="AAPL",
        side="Buy",
        raw_shares=2.0,
        raw_price_usd=100.0,
        commission_usd=1.0,
        refresh_calculated_data=False,
    )

    transaction_service.add_trade(
        date="2024-01-02",
        time_value="10:05:00",
        ticker="MSFT",
        side="Buy",
        raw_shares=1.0,
        raw_price_usd=200.0,
        commission_usd=1.0,
        refresh_calculated_data=False,
    )

    transaction_service.add_dividend(
        date="2024-01-15",
        ticker="AAPL",
        net_dividend_usd=2.0,
        refresh_calculated_data=False,
    )

    transaction_service.add_dividend(
        date="2024-01-22",
        ticker="MSFT",
        net_dividend_usd=1.5,
        refresh_calculated_data=False,
    )

    transaction_service.add_trade(
        date="2024-02-01",
        time_value="15:00:00",
        ticker="AAPL",
        side="Sell",
        raw_shares=1.0,
        raw_price_usd=110.0,
        commission_usd=1.0,
        refresh_calculated_data=False,
    )


@pytest.fixture
def integrated_portfolio(
    isolated_database,
    no_split_history,
    monkeypatch,
):
    """
    Build a complete isolated portfolio and run the
    calculated-data refresh chain.
    """

    install_fake_asset_refresh(
        monkeypatch
    )

    install_fake_market_refresh(
        monkeypatch
    )

    seed_portfolio_transactions()

    refresh_result = (
        portfolio_refresh_service
        .run_portfolio_refresh(
            progress_callback=None
        )
    )

    return {
        "database_path":
            isolated_database,
        "refresh_result":
            refresh_result,
    }


def test_complete_refresh_pipeline(
    integrated_portfolio,
):
    """
    The complete backend refresh chain must produce
    reconciled holdings and daily tables.
    """

    refresh_result = (
        integrated_portfolio[
            "refresh_result"
        ]
    )

    assert set(
        refresh_result
        .stage_results
        .keys()
    ) == {
        "asset_metadata",
        "market_data",
        "holdings_summary",
        "portfolio_daily",
        "security_daily",
        "validation",
    }

    assert (
        refresh_result
        .stage_results[
            "portfolio_daily"
        ][
            "is_reconciled"
        ]
        is True
    )

    holdings = (
        database.fetch_all(
            """
            SELECT *
            FROM holdings_summary
            ORDER BY ticker
            """
        )
    )

    holdings_table = pd.DataFrame(
        holdings
    )

    assert set(
        holdings_table["ticker"]
    ) == {
        "AAPL",
        "MSFT",
        "CASH",
    }

    aapl = (
        holdings_table
        .set_index("ticker")
        .loc["AAPL"]
    )

    msft = (
        holdings_table
        .set_index("ticker")
        .loc["MSFT"]
    )

    cash = (
        holdings_table
        .set_index("ticker")
        .loc["CASH"]
    )

    assert aapl["shares"] == pytest.approx(
        1.0
    )

    assert msft["shares"] == pytest.approx(
        1.0
    )

    # 1000 - 201 - 201 + 2 + 1.5 + 109
    assert cash[
        "market_value_usd"
    ] == pytest.approx(
        710.5
    )

    # AAPL cost per share after commission:
    # (2 * 100 + 1) / 2 = 100.5
    #
    # Sale proceeds after commission:
    # 1 * 110 - 1 = 109
    #
    # Realized P&L:
    # 109 - 100.5 = 8.5
    assert aapl[
        "realized_pnl_usd"
    ] == pytest.approx(
        8.5
    )

    assert aapl[
        "remaining_cost_basis_usd"
    ] == pytest.approx(
        100.5
    )


def test_portfolio_daily_matches_holdings(
    integrated_portfolio,
):
    """
    The final portfolio_daily value must equal the
    holdings_summary portfolio value.
    """

    latest_daily = (
        database.fetch_one(
            """
            SELECT *
            FROM portfolio_daily
            ORDER BY date DESC
            LIMIT 1
            """
        )
    )

    holdings_total = (
        database.fetch_one(
            """
            SELECT
                SUM(
                    market_value_usd
                ) AS portfolio_value_usd
            FROM holdings_summary
            """
        )
    )

    assert latest_daily is not None
    assert holdings_total is not None

    assert latest_daily[
        "closing_value_usd"
    ] == pytest.approx(
        holdings_total[
            "portfolio_value_usd"
        ]
    )

    assert np.isfinite(
        latest_daily[
            "daily_return"
        ]
    )

    assert np.isfinite(
        latest_daily[
            "cumulative_twr"
        ]
    )


def test_security_daily_matches_holdings(
    integrated_portfolio,
):
    """
    Final security shares and values must match
    holdings_summary.
    """

    rows = database.fetch_all(
        """
        WITH latest AS (
            SELECT
                ticker,
                date,
                closing_shares,
                closing_value_usd,
                cumulative_twr,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY date DESC
                ) AS row_number
            FROM security_daily
        )

        SELECT
            l.ticker,
            l.date,
            l.closing_shares,
            l.closing_value_usd,
            l.cumulative_twr,
            h.shares AS expected_shares,
            h.market_value_usd
                AS expected_value_usd

        FROM latest AS l

        INNER JOIN holdings_summary AS h
          ON h.ticker = l.ticker

        WHERE l.row_number = 1

        ORDER BY l.ticker
        """
    )

    results = pd.DataFrame(rows)

    assert set(
        results["ticker"]
    ) == {
        "AAPL",
        "MSFT",
    }

    for row in results.itertuples(
        index=False
    ):
        assert row.closing_shares == pytest.approx(
            row.expected_shares
        )

        assert row.closing_value_usd == pytest.approx(
            row.expected_value_usd
        )

        assert np.isfinite(
            row.cumulative_twr
        )


def test_performance_uses_security_daily(
    integrated_portfolio,
):
    """
    The performance table must use the final
    security_daily cumulative TWR.
    """

    performance = (
        performance_calculator
        .build_security_performance_table(
            risk_free_rate=0.04
        )
    )

    assert {
        "AAPL",
        "MSFT",
        "SPY",
    }.issubset(
        set(
            performance["ticker"]
        )
    )

    for ticker in [
        "AAPL",
        "MSFT",
    ]:
        performance_twr = float(
            performance.loc[
                performance["ticker"]
                == ticker,
                "security_twr",
            ].iloc[0]
        )

        latest_security = (
            database.fetch_one(
                """
                SELECT cumulative_twr
                FROM security_daily
                WHERE ticker = ?
                ORDER BY date DESC
                LIMIT 1
                """,
                (ticker,),
            )
        )

        assert latest_security is not None

        assert performance_twr == pytest.approx(
            latest_security[
                "cumulative_twr"
            ]
        )


def test_risk_analysis_integration(
    integrated_portfolio,
):
    """
    Risk analysis must run from the generated daily
    portfolio and market-price data.
    """

    result = (
        risk_calculator
        .build_risk_analysis(
            risk_free_rate=0.04
        )
    )

    summary = result.summary

    assert (
        summary[
            "portfolio_observations"
        ]
        >= 30
    )

    assert (
        summary[
            "open_security_count"
        ]
        == 2
    )

    assert np.isfinite(
        summary[
            "cumulative_portfolio_twr"
        ]
    )

    assert np.isfinite(
        summary[
            "annualized_portfolio_volatility"
        ]
    )

    assert set(
        result.correlation_matrix.columns
    ) == {
        "AAPL",
        "MSFT",
    }

    assert set(
        result.covariance_matrix.columns
    ) == {
        "AAPL",
        "MSFT",
    }


def test_optimizer_integration(
    integrated_portfolio,
):
    """
    Portfolio optimization must produce feasible
    allocations and an efficient frontier.
    """

    result = (
        portfolio_optimizer
        .build_portfolio_optimizer(
            lookback_years=3,
            minimum_observations=30,
            min_weight=0.0,
            max_weight=0.80,
            cash_reserve=None,
            frontier_points=10,
            risk_free_rate=0.04,
            selected_tickers=[
                "AAPL",
                "MSFT",
            ],
        )
    )

    assert set(
        result.summary[
            "eligible_tickers"
        ]
    ) == {
        "AAPL",
        "MSFT",
    }

    target_columns = [
        "equal_weight",
        "minimum_variance_weight",
        "maximum_sharpe_weight",
    ]

    for column in target_columns:
        assert result.allocation[
            column
        ].sum() == pytest.approx(
            1.0,
            abs=1e-6,
        )

        assert (
            result.allocation[
                column
            ]
            >= -1e-10
        ).all()

    assert not (
        result.efficient_frontier.empty
    )

    assert not (
        result.efficient_frontier_weights.empty
    )

    assert len(
        result.efficient_frontier
    ) >= 2


def test_rebalancing_integration(
    integrated_portfolio,
):
    """
    Rebalancing must produce valid actions from
    the optimizer output.
    """

    result = (
        rebalancing_calculator
        .build_rebalancing_plan(
            strategy="maximum_sharpe",
            commission_per_trade=0.0,
            minimum_trade_amount=0.0,
            fractional_shares=True,
            selected_tickers=[
                "AAPL",
                "MSFT",
            ],
            lookback_years=3,
            minimum_observations=30,
            min_weight=0.0,
            max_weight=0.80,
            cash_reserve=None,
            frontier_points=10,
            risk_free_rate=0.04,
        )
    )

    assert not (
        result.recommendations.empty
    )

    allowed_actions = {
        "BUY",
        "SELL",
        "HOLD",
        "CASH_RESERVE",
    }

    assert set(
        result.recommendations[
            "action"
        ]
    ).issubset(
        allowed_actions
    )

    assert np.isfinite(
        result.summary[
            "gross_buy_value_usd"
        ]
    )

    assert np.isfinite(
        result.summary[
            "gross_sell_value_usd"
        ]
    )

    assert np.isfinite(
        result.summary[
            "projected_cash_usd"
        ]
    )


def test_transaction_triggers_complete_refresh(
    integrated_portfolio,
    no_split_history,
):
    """
    Adding a trade with refresh enabled must update
    all calculated tables.
    """

    before_holding = (
        database.fetch_one(
            """
            SELECT shares
            FROM holdings_summary
            WHERE ticker = 'AAPL'
            """
        )
    )

    assert before_holding is not None

    result = (
        transaction_service.add_trade(
            date="2024-01-03",
            time_value="12:00:00",
            ticker="AAPL",
            side="Buy",
            raw_shares=0.5,
            raw_price_usd=101.0,
            commission_usd=0.0,
            refresh_calculated_data=True,
            progress_callback=None,
        )
    )

    assert result.refresh_result is not None

    after_holding = (
        database.fetch_one(
            """
            SELECT shares
            FROM holdings_summary
            WHERE ticker = 'AAPL'
            """
        )
    )

    assert after_holding is not None

    assert after_holding[
        "shares"
    ] == pytest.approx(
        before_holding["shares"]
        + 0.5
    )

    latest_security = (
        database.fetch_one(
            """
            SELECT closing_shares
            FROM security_daily
            WHERE ticker = 'AAPL'
            ORDER BY date DESC
            LIMIT 1
            """
        )
    )

    assert latest_security is not None

    assert latest_security[
        "closing_shares"
    ] == pytest.approx(
        after_holding[
            "shares"
        ]
    )

    latest_daily = (
        database.fetch_one(
            """
            SELECT closing_value_usd
            FROM portfolio_daily
            ORDER BY date DESC
            LIMIT 1
            """
        )
    )

    holdings_total = (
        database.fetch_one(
            """
            SELECT
                SUM(
                    market_value_usd
                ) AS portfolio_value_usd
            FROM holdings_summary
            """
        )
    )

    assert latest_daily is not None
    assert holdings_total is not None

    assert latest_daily[
        "closing_value_usd"
    ] == pytest.approx(
        holdings_total[
            "portfolio_value_usd"
        ]
    )
def test_portfolio_validation_passes_for_consistent_data(
    integrated_portfolio,
):
    """
    A fully refreshed portfolio must pass
    every critical check.
    """

    result = (
        portfolio_validation_service
        .run_portfolio_validation()
    )

    assert result.is_valid is True

    assert result.summary[
        "failed_checks"
    ] == 0

    assert result.summary[
        "total_checks"
    ] == 11

    assert "FAIL" not in set(
        result.checks["status"]
    )


def test_portfolio_validation_detects_cash_difference(
    integrated_portfolio,
):
    """
    A corrupted CASH holding must fail
    cash reconciliation.
    """

    with database.database_transaction() as connection:
        connection.execute(
            """
            UPDATE holdings_summary
            SET market_value_usd =
                market_value_usd + 25
            WHERE ticker = 'CASH'
            """
        )

    result = (
        portfolio_validation_service
        .run_portfolio_validation()
    )

    check = (
        result.checks
        .set_index("check_key")
        .loc["cash_reconciliation"]
    )

    assert result.is_valid is False

    assert check[
        "status"
    ] == "FAIL"

    assert check[
        "details"
    ][
        "difference_usd"
    ] == pytest.approx(
        -25.0
    )


def test_portfolio_validation_detects_missing_open_prices(
    integrated_portfolio,
):
    """
    An open holding without price history
    must fail validation.
    """

    with database.database_transaction() as connection:
        connection.execute(
            """
            DELETE FROM market_prices
            WHERE ticker = 'AAPL'
            """
        )

    result = (
        portfolio_validation_service
        .run_portfolio_validation()
    )

    check = (
        result.checks
        .set_index("check_key")
        .loc["open_market_prices"]
    )

    assert result.is_valid is False

    assert check[
        "status"
    ] == "FAIL"

    missing_tickers = {
        row["ticker"]
        for row in check[
            "details"
        ][
            "missing_prices"
        ]
    }

    assert "AAPL" in missing_tickers
