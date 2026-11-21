from datetime import date
from io import BytesIO

import pandas as pd
import pytest

import excel_importer
from midas_action_confirmation_service import (
    ActionConfirmationResult,
)
from midas_import_service import (
    CombinedMidasStatements,
)
from midas_pdf_parser import (
    CASH_MOVEMENT_COLUMNS,
    DIVIDEND_COLUMNS,
    HOLDING_COLUMNS,
    TRADE_COLUMNS,
    UNSUPPORTED_ACTIVITY_COLUMNS,
)
from midas_portfolio_exporter import (
    apply_ticker_normalization,
    build_verified_ticker_alias_map,
    build_midas_portfolio_workbook,
    normalize_portfolio_ticker,
    save_midas_portfolio_workbook,
)


def empty_frame(
    columns: list[str],
) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def build_source_trades() -> pd.DataFrame:
    rows = [
        {
            "date": date(2022, 2, 7),
            "time": "10:00:00",
            "ticker": "TWTR",
            "side": "Buy",
            "shares": 0.221877,
            "price_usd": 40.0,
            "commission_usd": 0.0,
            "gross_amount_usd": 8.87508,
            "is_gift": False,
            "source_file": "February.pdf",
            "source_hash": "source-buy",
            "statement_start": date(2022, 2, 1),
            "statement_end": date(2022, 2, 28),
            "source_page": 1,
        },
        {
            "date": date(2022, 10, 28),
            "time": None,
            "ticker": "TWTR",
            "side": "Sell",
            "shares": 0.221877,
            "price_usd": 54.20,
            "commission_usd": 0.0,
            "gross_amount_usd": 12.0257334,
            "is_gift": False,
            "source_file": (
                "SEC_CORPORATE_ACTION"
            ),
            "source_hash": (
                "midas-action-test"
            ),
            "statement_start": date(2022, 10, 28),
            "statement_end": date(2022, 10, 28),
            "source_page": None,
        },
    ]

    return pd.DataFrame(
        rows,
        columns=TRADE_COLUMNS,
    )


def build_statements() -> CombinedMidasStatements:
    cash_movements = pd.DataFrame(
        [
            {
                "date": date(2022, 11, 3),
                "time": "12:00:00",
                "type": "WITHDRAWAL",
                "amount_usd": 12.03,
                "description": "FX sale",
                "request_date": date(2022, 11, 3),
                "request_time": "12:00:00",
                "source_file": "November.pdf",
                "source_hash": "cash-source",
                "statement_start": date(2022, 11, 1),
                "statement_end": date(2022, 11, 30),
                "source_page": 1,
            }
        ],
        columns=CASH_MOVEMENT_COLUMNS,
    )

    dividends = pd.DataFrame(
        [
            {
                "date": date(2022, 6, 1),
                "time": None,
                "ticker": "TWTR",
                "company_name": "Twitter, Inc.",
                "gross_dividend_usd": 1.0,
                "withholding_tax_usd": 0.15,
                "net_dividend_usd": 0.85,
                "source_file": "June.pdf",
                "source_hash": "dividend-source",
                "statement_start": date(2022, 6, 1),
                "statement_end": date(2022, 6, 30),
                "source_page": 1,
            }
        ],
        columns=DIVIDEND_COLUMNS,
    )

    return CombinedMidasStatements(
        trades=build_source_trades(),
        cash_movements=cash_movements,
        dividends=dividends,
        holdings=empty_frame(
            HOLDING_COLUMNS
        ),
        unsupported_activities=empty_frame(
            UNSUPPORTED_ACTIVITY_COLUMNS
        ),
        statement_summaries=[],
        duplicate_pdf_names=[],
        duplicates_removed={},
        ignored_order_count=0,
        warnings=[],
    )


def build_confirmation(
    status: str = "READY_FOR_EXPORT",
) -> ActionConfirmationResult:
    return ActionConfirmationResult(
        review_actions=pd.DataFrame(),
        approved_actions=pd.DataFrame(),
        rejected_actions=pd.DataFrame(),
        combined_trades=build_source_trades(),
        summary={
            "status": status,
            "approved_count": 1,
        },
    )


def test_workbook_contains_required_sheets() -> None:
    result = build_midas_portfolio_workbook(
        statements=build_statements(),
        confirmation=build_confirmation(),
    )

    excel_file = pd.ExcelFile(
        BytesIO(result.workbook_bytes),
        engine="openpyxl",
    )

    assert set(excel_file.sheet_names) == {
        "trades",
        "cash_movements",
        "dividends",
    }

    assert result.summary["status"] == (
        "READY_FOR_DOWNLOAD"
    )

    assert result.summary["trade_count"] == 2

    assert result.summary[
        "cash_movement_count"
    ] == 1

    assert result.summary[
        "dividend_count"
    ] == 1


def test_workbook_is_accepted_by_excel_importer(
    tmp_path,
    monkeypatch,
) -> None:
    result = build_midas_portfolio_workbook(
        statements=build_statements(),
        confirmation=build_confirmation(),
    )

    output_path = (
        tmp_path / "portfolio.xlsx"
    )

    output_path.write_bytes(
        result.workbook_bytes
    )

    monkeypatch.setattr(
        excel_importer,
        "download_split_history",
        lambda tickers: {
            ticker: []
            for ticker in tickers
        },
    )

    trades, cash_movements, dividends = (
        excel_importer.read_portfolio_excel(
            output_path
        )
    )

    assert len(trades) == 2
    assert len(cash_movements) == 1
    assert len(dividends) == 1

    assert trades.iloc[-1]["ticker"] == "TWTR"
    assert trades.iloc[-1]["side"] == "Sell"


def test_export_requires_completed_review() -> None:
    with pytest.raises(
        ValueError,
        match="not ready for export",
    ):
        build_midas_portfolio_workbook(
            statements=build_statements(),
            confirmation=build_confirmation(
                status="REVIEW_REQUIRED"
            ),
        )


def test_save_does_not_overwrite_by_default(
    tmp_path,
) -> None:
    result = build_midas_portfolio_workbook(
        statements=build_statements(),
        confirmation=build_confirmation(),
    )

    output_path = (
        tmp_path / "portfolio.xlsx"
    )

    output_path.write_bytes(
        b"existing-file"
    )

    with pytest.raises(
        FileExistsError,
        match="already exists",
    ):
        save_midas_portfolio_workbook(
            workbook=result,
            destination=output_path,
        )

    assert output_path.read_bytes() == (
        b"existing-file"
    )


def test_save_allows_explicit_overwrite(
    tmp_path,
) -> None:
    result = build_midas_portfolio_workbook(
        statements=build_statements(),
        confirmation=build_confirmation(),
    )

    output_path = (
        tmp_path / "portfolio.xlsx"
    )

    output_path.write_bytes(
        b"existing-file"
    )

    saved_path = save_midas_portfolio_workbook(
        workbook=result,
        destination=output_path,
        overwrite=True,
    )

    assert saved_path == output_path

    assert output_path.read_bytes() == (
        result.workbook_bytes
    )


def test_class_share_ticker_is_normalized_generically() -> None:
    assert normalize_portfolio_ticker(
        "brk.b"
    ) == "BRK-B"


def test_only_verified_ticker_aliases_are_applied() -> None:
    ticker_aliases = pd.DataFrame(
        [
            {
                "old_ticker": "ABL",
                "new_ticker": "ABX",
                "auto_apply": True,
            },
            {
                "old_ticker": "OLD",
                "new_ticker": "MID",
                "auto_apply": True,
            },
            {
                "old_ticker": "MID",
                "new_ticker": "NEW",
                "auto_apply": True,
            },
            {
                "old_ticker": "PENDING",
                "new_ticker": "NOPE",
                "auto_apply": False,
            },
        ]
    )

    alias_map = build_verified_ticker_alias_map(
        ticker_aliases
    )

    assert alias_map == {
        "ABL": "ABX",
        "OLD": "NEW",
        "MID": "NEW",
    }

    normalized = apply_ticker_normalization(
        dataframe=pd.DataFrame(
            {
                "ticker": [
                    "ABL",
                    "brk.b",
                    "PENDING",
                    "OLD",
                ]
            }
        ),
        alias_map=alias_map,
    )

    assert normalized["ticker"].tolist() == [
        "ABX",
        "BRK-B",
        "PENDING",
        "NEW",
    ]


def test_workbook_applies_verified_aliases_to_all_ticker_tables() -> None:
    statements = build_statements()
    confirmation = build_confirmation()

    statements.dividends.loc[
        0,
        "ticker",
    ] = "ABL"

    confirmation.combined_trades.loc[
        0,
        "ticker",
    ] = "ABL"

    result = build_midas_portfolio_workbook(
        statements=statements,
        confirmation=confirmation,
        ticker_aliases=pd.DataFrame(
            [
                {
                    "old_ticker": "ABL",
                    "new_ticker": "ABX",
                    "auto_apply": True,
                }
            ]
        ),
    )

    assert result.trades.iloc[0][
        "ticker"
    ] == "ABX"

    assert result.dividends.iloc[0][
        "ticker"
    ] == "ABX"

    assert result.summary[
        "applied_ticker_alias_count"
    ] == 1
