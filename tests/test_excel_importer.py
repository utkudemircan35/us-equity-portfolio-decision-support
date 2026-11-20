from pathlib import Path

import pandas as pd
import pytest

import database
import excel_importer


def write_test_workbook(
    path: Path,
    trades: pd.DataFrame,
    cash_movements: pd.DataFrame,
    dividends: pd.DataFrame,
) -> None:
    """
    Write the three required portfolio sheets.
    """

    with pd.ExcelWriter(
        path,
        engine="openpyxl",
    ) as writer:
        trades.to_excel(
            writer,
            sheet_name="trades",
            index=False,
        )

        cash_movements.to_excel(
            writer,
            sheet_name="cash_movements",
            index=False,
        )

        dividends.to_excel(
            writer,
            sheet_name="dividends",
            index=False,
        )


def build_valid_workbook(
    tmp_path: Path,
) -> Path:
    """
    Create a representative user workbook.
    """

    workbook_path = (
        tmp_path
        / "portfolio.xlsx"
    )

    trades = pd.DataFrame(
        [
            {
                "date": "2024-01-02",
                "time": None,
                "ticker": "aapl",
                "side": "buy",
                "shares": 2.123456789,
                "price_usd": 100.123456789,
                "commission_usd": 1.23456789,
                "is_gift": None,
            },
            {
                "date": "2024-01-03",
                "time": "10:15:00",
                "ticker": "msft",
                "side": "Buy",
                "shares": 0.25,
                "price_usd": 200.0,
                "commission_usd": 0.0,
                "is_gift": True,
            },
            {
                "date": "2024-02-01",
                "time": "15:30:00",
                "ticker": "AAPL",
                "side": "Sell",
                "shares": 0.5,
                "price_usd": 110.0,
                "commission_usd": 0.5,
                "is_gift": False,
            },
        ]
    )

    cash_movements = pd.DataFrame(
        [
            {
                "date": "2024-01-02",
                "time": None,
                "type": "deposit",
                "amount_usd": 1000.123456789,
            },
            {
                "date": "2024-01-31",
                "time": "09:30:00",
                "type": "interest",
                "amount_usd": 1.23456789,
            },
        ]
    )

    dividends = pd.DataFrame(
        [
            {
                "date": "2024-01-15",
                "time": None,
                "ticker": "aapl",
                "net_dividend_usd": 1.23456789,
            }
        ]
    )

    write_test_workbook(
        path=workbook_path,
        trades=trades,
        cash_movements=(
            cash_movements
        ),
        dividends=dividends,
    )

    return workbook_path


@pytest.fixture
def no_importer_splits(
    monkeypatch,
):
    """
    Disable network split downloads
    for importer tests.
    """

    monkeypatch.setattr(
        excel_importer,
        "download_split_history",
        lambda tickers: {
            ticker: []
            for ticker in tickers
        },
    )


def test_excel_import_preserves_raw_values_and_creates_ids(
    isolated_database,
    no_importer_splits,
    tmp_path,
):
    """
    Import source values without rounding and
    calculate derived fields.
    """

    workbook_path = (
        build_valid_workbook(
            tmp_path
        )
    )

    result = (
        excel_importer
        .import_excel_to_database(
            excel_path=workbook_path,
            replace_existing=False,
        )
    )

    assert result == {
        "trades": 3,
        "cash_movements": 2,
        "dividends": 1,
    }

    trades = database.fetch_all(
        """
        SELECT *
        FROM trades
        ORDER BY trade_id
        """
    )

    assert [
        row["trade_id"]
        for row in trades
    ] == [1, 2, 3]

    first_trade = trades[0]
    gift_trade = trades[1]
    sale = trades[2]

    assert first_trade["time"] is None
    assert first_trade["ticker"] == "AAPL"

    assert first_trade[
        "raw_shares"
    ] == pytest.approx(
        2.123456789
    )

    assert first_trade[
        "raw_price_usd"
    ] == pytest.approx(
        100.123456789
    )

    assert first_trade[
        "shares"
    ] == pytest.approx(
        2.123456789
    )

    assert first_trade[
        "price_usd"
    ] == pytest.approx(
        100.123456789
    )

    expected_buy_flow = -(
        2.123456789
        * 100.123456789
        + 1.23456789
    )

    assert first_trade[
        "net_flow_usd"
    ] == pytest.approx(
        expected_buy_flow
    )

    assert first_trade[
        "cumulative_shares"
    ] == pytest.approx(
        2.123456789
    )

    assert gift_trade[
        "ticker"
    ] == "MSFT"

    assert gift_trade[
        "is_gift"
    ] == 1

    assert gift_trade[
        "net_flow_usd"
    ] == pytest.approx(
        0.0
    )

    assert gift_trade[
        "cumulative_shares"
    ] == pytest.approx(
        0.25
    )

    assert sale[
        "net_flow_usd"
    ] == pytest.approx(
        54.5
    )

    assert sale[
        "cumulative_shares"
    ] == pytest.approx(
        1.623456789
    )

    cash = database.fetch_all(
        """
        SELECT *
        FROM cash_movements
        ORDER BY cash_id
        """
    )

    assert cash[0]["cash_id"] == 1
    assert cash[0]["time"] is None
    assert cash[0]["type"] == "DEPOSIT"

    assert cash[0][
        "amount_usd"
    ] == pytest.approx(
        1000.123456789
    )

    dividends = database.fetch_all(
        """
        SELECT *
        FROM dividends
        ORDER BY dividend_id
        """
    )

    assert dividends[0][
        "dividend_id"
    ] == 1

    assert dividends[0][
        "time"
    ] is None

    assert dividends[0][
        "ticker"
    ] == "AAPL"

    assert dividends[0][
        "net_dividend_usd"
    ] == pytest.approx(
        1.23456789
    )


def test_excel_import_applies_splits_and_preserves_raw_values(
    isolated_database,
    monkeypatch,
    tmp_path,
):
    """
    Adjust calculated shares and prices while
    retaining source values.
    """

    monkeypatch.setattr(
        excel_importer,
        "download_split_history",
        lambda tickers: {
            "TEST": [
                (
                    pd.Timestamp(
                        "2024-06-10"
                    ),
                    4.0,
                )
            ]
        },
    )

    workbook_path = (
        tmp_path
        / "split_portfolio.xlsx"
    )

    write_test_workbook(
        path=workbook_path,
        trades=pd.DataFrame(
            [
                {
                    "date": "2023-01-02",
                    "time": None,
                    "ticker": "TEST",
                    "side": "Buy",
                    "shares": 1.0,
                    "price_usd": 400.0,
                    "commission_usd": 0.0,
                }
            ]
        ),
        cash_movements=pd.DataFrame(
            [
                {
                    "date": "2023-01-02",
                    "time": None,
                    "type": "DEPOSIT",
                    "amount_usd": 400.0,
                }
            ]
        ),
        dividends=pd.DataFrame(
            columns=[
                "date",
                "time",
                "ticker",
                "net_dividend_usd",
            ]
        ),
    )

    excel_importer.import_excel_to_database(
        excel_path=workbook_path,
    )

    trade = database.fetch_one(
        """
        SELECT *
        FROM trades
        WHERE trade_id = 1
        """
    )

    assert trade is not None

    assert trade[
        "raw_shares"
    ] == pytest.approx(
        1.0
    )

    assert trade[
        "raw_price_usd"
    ] == pytest.approx(
        400.0
    )

    assert trade[
        "split_factor"
    ] == pytest.approx(
        4.0
    )

    assert trade[
        "shares"
    ] == pytest.approx(
        4.0
    )

    assert trade[
        "price_usd"
    ] == pytest.approx(
        100.0
    )

    assert trade[
        "net_flow_usd"
    ] == pytest.approx(
        -400.0
    )


def test_second_import_requires_explicit_replacement(
    isolated_database,
    no_importer_splits,
    tmp_path,
):
    """
    Protect existing data unless full
    replacement is requested.
    """

    workbook_path = (
        build_valid_workbook(
            tmp_path
        )
    )

    excel_importer.import_excel_to_database(
        excel_path=workbook_path,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "already contains "
            "transaction data"
        ),
    ):
        excel_importer.import_excel_to_database(
            excel_path=workbook_path,
            replace_existing=False,
        )

    excel_importer.import_excel_to_database(
        excel_path=workbook_path,
        replace_existing=True,
    )

    counts = database.fetch_one(
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

    assert counts == {
        "trades": 3,
        "cash_movements": 2,
        "dividends": 1,
    }

    first_trade = database.fetch_one(
        """
        SELECT
            MIN(trade_id) AS first_id
        FROM trades
        """
    )

    assert first_trade == {
        "first_id": 1
    }


def test_missing_required_sheet_is_rejected(
    isolated_database,
    tmp_path,
):
    """
    Reject workbooks that do not contain
    all required sheets.
    """

    workbook_path = (
        tmp_path
        / "missing_sheet.xlsx"
    )

    with pd.ExcelWriter(
        workbook_path,
        engine="openpyxl",
    ) as writer:
        pd.DataFrame(
            columns=(
                excel_importer
                .TRADE_COLUMNS
            )
        ).to_excel(
            writer,
            sheet_name="trades",
            index=False,
        )

        pd.DataFrame(
            columns=(
                excel_importer
                .CASH_MOVEMENT_COLUMNS
            )
        ).to_excel(
            writer,
            sheet_name=(
                "cash_movements"
            ),
            index=False,
        )

    with pytest.raises(
        ValueError,
        match="missing required sheets",
    ):
        excel_importer.import_excel_to_database(
            excel_path=workbook_path,
        )


def test_gift_commission_is_rejected(
    isolated_database,
    no_importer_splits,
):
    """
    Do not silently discard commissions
    entered for gift shares.
    """

    trades = pd.DataFrame(
        [
            {
                "date": "2024-01-02",
                "time": None,
                "ticker": "MSFT",
                "side": "Buy",
                "shares": 1.0,
                "price_usd": 100.0,
                "commission_usd": 1.0,
                "is_gift": True,
            }
        ]
    )

    with pytest.raises(
        ValueError,
        match=(
            "Gift transactions "
            "cannot have a commission"
        ),
    ):
        excel_importer.prepare_trades(
            trades
        )


def test_overselling_is_rejected_before_database_write(
    isolated_database,
    no_importer_splits,
):
    """
    Reject Excel histories that create
    a negative share balance.
    """

    trades = pd.DataFrame(
        [
            {
                "date": "2024-01-02",
                "time": None,
                "ticker": "AAPL",
                "side": "Buy",
                "shares": 1.0,
                "price_usd": 100.0,
                "commission_usd": 0.0,
            },
            {
                "date": "2024-01-03",
                "time": None,
                "ticker": "AAPL",
                "side": "Sell",
                "shares": 2.0,
                "price_usd": 110.0,
                "commission_usd": 0.0,
            },
        ]
    )

    with pytest.raises(
        ValueError,
        match=(
            "negative share balance"
        ),
    ):
        excel_importer.prepare_trades(
            trades
        )
