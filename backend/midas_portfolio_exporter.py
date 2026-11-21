from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from midas_action_confirmation_service import (
    ActionConfirmationResult,
)
from midas_import_service import (
    CombinedMidasStatements,
)


EXCEL_TRADE_COLUMNS = [
    "date",
    "time",
    "ticker",
    "side",
    "shares",
    "price_usd",
    "commission_usd",
    "is_gift",
]

EXCEL_CASH_MOVEMENT_COLUMNS = [
    "date",
    "time",
    "type",
    "amount_usd",
]

EXCEL_DIVIDEND_COLUMNS = [
    "date",
    "time",
    "ticker",
    "net_dividend_usd",
]


@dataclass
class MidasPortfolioWorkbook:
    """
    Frontend-ready portfolio workbook and its source tables.
    """

    workbook_bytes: bytes
    trades: pd.DataFrame
    cash_movements: pd.DataFrame
    dividends: pd.DataFrame
    summary: dict[str, Any]


def validate_columns(
    dataframe: pd.DataFrame,
    required_columns: list[str],
    table_name: str,
) -> None:
    """
    Ensure that an export source contains every required field.
    """

    missing_columns = [
        column
        for column in required_columns
        if column not in dataframe.columns
    ]

    if missing_columns:
        raise ValueError(
            f"{table_name} is missing required columns: "
            + ", ".join(missing_columns)
        )


def normalize_export_dates(
    dataframe: pd.DataFrame,
    table_name: str,
) -> pd.DataFrame:
    """
    Convert export dates to date values accepted by Excel.
    """

    result = dataframe.copy()

    converted = pd.to_datetime(
        result["date"],
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
            f"{table_name} contains invalid dates "
            "at rows: "
            + ", ".join(row_numbers)
        )

    result["date"] = converted.dt.date

    return result


def normalize_portfolio_ticker(
    value: Any,
) -> str:
    """
    Normalize a US ticker for Yahoo-compatible portfolio use.

    Class-share tickers such as BRK.B become BRK-B.
    """

    ticker = str(value).strip().upper()

    if not ticker:
        raise ValueError(
            "Ticker cannot be empty."
        )

    return ticker.replace(".", "-")


def boolean_flag(
    value: Any,
) -> bool:
    """
    Normalize a boolean-like dataframe value.
    """

    if pd.isna(value):
        return False

    if isinstance(value, bool):
        return value

    return str(value).strip().casefold() in {
        "1",
        "true",
        "yes",
        "y",
    }


def build_verified_ticker_alias_map(
    ticker_aliases: pd.DataFrame | None,
) -> dict[str, str]:
    """
    Build a map from automatically verified ticker changes.

    Unverified candidates are never applied.
    """

    if (
        ticker_aliases is None
        or ticker_aliases.empty
    ):
        return {}

    required_columns = {
        "old_ticker",
        "new_ticker",
        "auto_apply",
    }

    missing_columns = sorted(
        required_columns
        - set(ticker_aliases.columns)
    )

    if missing_columns:
        raise ValueError(
            "Ticker aliases are missing required "
            "columns: "
            + ", ".join(missing_columns)
        )

    direct_map: dict[str, str] = {}

    for _, row in ticker_aliases.iterrows():
        if not boolean_flag(row["auto_apply"]):
            continue

        old_ticker = normalize_portfolio_ticker(
            row["old_ticker"]
        )

        new_ticker = normalize_portfolio_ticker(
            row["new_ticker"]
        )

        existing_target = direct_map.get(
            old_ticker
        )

        if (
            existing_target is not None
            and existing_target != new_ticker
        ):
            raise ValueError(
                "Conflicting verified ticker aliases "
                f"for {old_ticker}."
            )

        if old_ticker != new_ticker:
            direct_map[old_ticker] = new_ticker

    resolved_map: dict[str, str] = {}

    for old_ticker in direct_map:
        current_ticker = old_ticker
        visited: set[str] = set()

        while current_ticker in direct_map:
            if current_ticker in visited:
                raise ValueError(
                    "A cycle was found in verified "
                    "ticker aliases."
                )

            visited.add(current_ticker)
            current_ticker = direct_map[
                current_ticker
            ]

        resolved_map[old_ticker] = (
            current_ticker
        )

    return resolved_map


def apply_ticker_normalization(
    dataframe: pd.DataFrame,
    alias_map: Mapping[str, str],
) -> pd.DataFrame:
    """
    Apply class-share formatting and verified aliases.
    """

    result = dataframe.copy()

    result["ticker"] = (
        result["ticker"]
        .map(normalize_portfolio_ticker)
        .map(
            lambda ticker: alias_map.get(
                ticker,
                ticker,
            )
        )
    )

    return result


def prepare_trade_export(
    confirmation: ActionConfirmationResult,
    alias_map: (
        Mapping[str, str] | None
    ) = None,
) -> pd.DataFrame:
    """
    Prepare approved and original trades for portfolio.xlsx.
    """

    trades = confirmation.combined_trades.copy()

    validate_columns(
        dataframe=trades,
        required_columns=EXCEL_TRADE_COLUMNS,
        table_name="trades",
    )

    trades = trades[
        EXCEL_TRADE_COLUMNS
    ].copy()

    trades = normalize_export_dates(
        dataframe=trades,
        table_name="trades",
    )

    trades = apply_ticker_normalization(
        dataframe=trades,
        alias_map=alias_map or {},
    )

    trades["side"] = (
        trades["side"]
        .astype(str)
        .str.strip()
        .str.title()
    )

    for column in [
        "shares",
        "price_usd",
        "commission_usd",
    ]:
        trades[column] = pd.to_numeric(
            trades[column],
            errors="raise",
        )

    trades["is_gift"] = (
        trades["is_gift"].astype(bool)
    )

    return trades.reset_index(drop=True)


def prepare_cash_movement_export(
    statements: CombinedMidasStatements,
) -> pd.DataFrame:
    """
    Prepare normalized cash movements for portfolio.xlsx.
    """

    cash_movements = (
        statements.cash_movements.copy()
    )

    validate_columns(
        dataframe=cash_movements,
        required_columns=(
            EXCEL_CASH_MOVEMENT_COLUMNS
        ),
        table_name="cash_movements",
    )

    cash_movements = cash_movements[
        EXCEL_CASH_MOVEMENT_COLUMNS
    ].copy()

    cash_movements = normalize_export_dates(
        dataframe=cash_movements,
        table_name="cash_movements",
    )

    cash_movements["type"] = (
        cash_movements["type"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    cash_movements["amount_usd"] = (
        pd.to_numeric(
            cash_movements["amount_usd"],
            errors="raise",
        )
    )

    return cash_movements.reset_index(
        drop=True
    )


def prepare_dividend_export(
    statements: CombinedMidasStatements,
    alias_map: (
        Mapping[str, str] | None
    ) = None,
) -> pd.DataFrame:
    """
    Prepare normalized dividends for portfolio.xlsx.
    """

    dividends = statements.dividends.copy()

    validate_columns(
        dataframe=dividends,
        required_columns=EXCEL_DIVIDEND_COLUMNS,
        table_name="dividends",
    )

    dividends = dividends[
        EXCEL_DIVIDEND_COLUMNS
    ].copy()

    dividends = normalize_export_dates(
        dataframe=dividends,
        table_name="dividends",
    )

    dividends = apply_ticker_normalization(
        dataframe=dividends,
        alias_map=alias_map or {},
    )

    dividends["net_dividend_usd"] = (
        pd.to_numeric(
            dividends["net_dividend_usd"],
            errors="raise",
        )
    )

    return dividends.reset_index(drop=True)


def create_workbook_bytes(
    trades: pd.DataFrame,
    cash_movements: pd.DataFrame,
    dividends: pd.DataFrame,
) -> bytes:
    """
    Create an in-memory workbook for Streamlit download.
    """

    output = BytesIO()

    with pd.ExcelWriter(
        output,
        engine="openpyxl",
        date_format="YYYY-MM-DD",
        datetime_format="YYYY-MM-DD HH:MM:SS",
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

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"

            worksheet.auto_filter.ref = (
                worksheet.dimensions
            )

    return output.getvalue()


def build_midas_portfolio_workbook(
    statements: CombinedMidasStatements,
    confirmation: ActionConfirmationResult,
    ticker_aliases: pd.DataFrame | None = None,
) -> MidasPortfolioWorkbook:
    """
    Build a portfolio.xlsx-compatible workbook in memory.
    """

    confirmation_status = (
        confirmation.summary.get("status")
    )

    if confirmation_status != "READY_FOR_EXPORT":
        raise ValueError(
            "Corporate-action review is not ready "
            "for export. Current status: "
            f"{confirmation_status}"
        )

    alias_map = (
        build_verified_ticker_alias_map(
            ticker_aliases
        )
    )

    trades = prepare_trade_export(
        confirmation=confirmation,
        alias_map=alias_map,
    )

    cash_movements = (
        prepare_cash_movement_export(
            statements
        )
    )

    dividends = prepare_dividend_export(
        statements=statements,
        alias_map=alias_map,
    )

    workbook_bytes = create_workbook_bytes(
        trades=trades,
        cash_movements=cash_movements,
        dividends=dividends,
    )

    summary = {
        "status": "READY_FOR_DOWNLOAD",
        "trade_count": len(trades),
        "cash_movement_count": len(
            cash_movements
        ),
        "dividend_count": len(dividends),
        "approved_action_count": (
            confirmation.summary.get(
                "approved_count",
                0,
            )
        ),
        "applied_ticker_alias_count": len(
            alias_map
        ),
        "workbook_size_bytes": len(
            workbook_bytes
        ),
    }

    return MidasPortfolioWorkbook(
        workbook_bytes=workbook_bytes,
        trades=trades,
        cash_movements=cash_movements,
        dividends=dividends,
        summary=summary,
    )


def save_midas_portfolio_workbook(
    workbook: MidasPortfolioWorkbook,
    destination: str | Path,
    overwrite: bool = False,
) -> Path:
    """
    Save a previously built workbook to disk.
    """

    output_path = Path(destination)

    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output file already exists: "
            f"{output_path}"
        )

    if not output_path.parent.exists():
        raise FileNotFoundError(
            f"Output folder does not exist: "
            f"{output_path.parent}"
        )

    output_path.write_bytes(
        workbook.workbook_bytes
    )

    return output_path
