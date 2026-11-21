from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from asset_metadata import refresh_asset_metadata
from database import (
    DATABASE_PATH,
    PROJECT_DIR,
)
from database_setup import create_database
from excel_importer import (
    DEFAULT_EXCEL_PATH,
    import_excel_to_database,
)
from market_data import refresh_market_data
from midas_pipeline_cli import (
    run_midas_pipeline,
)
from portfolio_calculator import refresh_holdings_summary
from portfolio_daily_calculator import (
    refresh_portfolio_daily,
)
from security_daily_calculator import (
    refresh_security_daily,
)

from portfolio_validation_service import (
    run_portfolio_validation,
)


DEFAULT_MIDAS_FOLDER = (
    PROJECT_DIR / "midas_ekstreleri"
)

DEFAULT_MIDAS_OUTPUT = (
    PROJECT_DIR
    / "midas_portfolio_generated.xlsx"
)


@dataclass(frozen=True)
class PipelineProgress:
    stage_index: int
    total_stages: int
    stage_key: str
    stage_name: str
    status: str
    message: str
    progress: float


@dataclass
class PortfolioPipelineResult:
    excel_path: Path
    database_path: Path
    stage_results: dict[str, Any]
    elapsed_seconds: float


ProgressCallback = Callable[[PipelineProgress], None]


def console_progress_callback(
    event: PipelineProgress,
) -> None:
    """
    Display pipeline progress in the terminal.
    """

    prefix = (
        f"[{event.stage_index}/{event.total_stages}]"
    )

    if event.status == "started":
        print(f"{prefix} {event.stage_name}...")

    elif event.status == "completed":
        print(
            f"{prefix} {event.message}"
        )

    elif event.status == "failed":
        print(
            f"{prefix} {event.stage_name}: "
            f"failed - {event.message}"
        )


def emit_progress(
    callback: ProgressCallback | None,
    stage_index: int,
    total_stages: int,
    stage_key: str,
    stage_name: str,
    status: str,
    message: str,
) -> None:
    """
    Send a progress event to the selected interface.
    """

    if callback is None:
        return

    if status == "completed":
        completed_fraction = (
            stage_index / total_stages
        )
    else:
        completed_fraction = (
            (stage_index - 1) / total_stages
        )

    callback(
        PipelineProgress(
            stage_index=stage_index,
            total_stages=total_stages,
            stage_key=stage_key,
            stage_name=stage_name,
            status=status,
            message=message,
            progress=min(
                max(completed_fraction, 0.0),
                1.0,
            ),
        )
    )


def run_stage(
    stage_index: int,
    total_stages: int,
    stage_key: str,
    stage_name: str,
    function: Callable[[], Any],
    callback: ProgressCallback | None,
) -> Any:
    """
    Run one pipeline stage and report its status.
    """

    stage_started = perf_counter()

    emit_progress(
        callback=callback,
        stage_index=stage_index,
        total_stages=total_stages,
        stage_key=stage_key,
        stage_name=stage_name,
        status="started",
        message=f"{stage_name} started.",
    )

    try:
        result = function()

    except Exception as error:
        emit_progress(
            callback=callback,
            stage_index=stage_index,
            total_stages=total_stages,
            stage_key=stage_key,
            stage_name=stage_name,
            status="failed",
            message=str(error),
        )

        raise

    elapsed_seconds = (
        perf_counter() - stage_started
    )

    emit_progress(
        callback=callback,
        stage_index=stage_index,
        total_stages=total_stages,
        stage_key=stage_key,
        stage_name=stage_name,
        status="completed",
        message=(
            f"{stage_name} completed in "
            f"{elapsed_seconds:.2f} seconds."
        ),
    )

    return result


def refresh_market_data_checked() -> dict[str, Any]:
    """
    Refresh market prices and stop if required prices fail.
    """

    result = refresh_market_data(
        include_benchmarks=True,
        clear_existing=False,
    )

    failed_tickers = result[
        "failed_required_tickers"
    ]

    if failed_tickers:
        failure_details = "; ".join(
            f"{ticker}: {reason}"
            for ticker, reason
            in failed_tickers.items()
        )

        raise RuntimeError(
            "Required market-price downloads failed: "
            + failure_details
        )

    return result


def refresh_portfolio_daily_checked() -> dict[str, Any]:
    """
    Refresh portfolio_daily and validate reconciliation.
    """

    result = refresh_portfolio_daily()

    if not result["is_reconciled"]:
        difference = result[
            "reconciliation_difference"
        ]

        raise RuntimeError(
            "Portfolio reconciliation failed. "
            f"Difference: {difference}"
        )

    return result


def validate_final_portfolio(
) -> dict[str, Any]:
    """
    Perform the final portfolio validation.
    """

    result = (
        run_portfolio_validation()
    )

    if not result.is_valid:
        failed_checks = (
            result.checks.loc[
                result.checks["status"]
                == "FAIL",
                "check_key",
            ]
            .tolist()
        )

        raise RuntimeError(
            "Portfolio validation failed: "
            + ", ".join(failed_checks)
        )

    return {
        "database_path": str(
            DATABASE_PATH
        ),
        "status": result.summary[
            "status"
        ],
        "passed_checks": result.summary[
            "passed_checks"
        ],
        "warning_checks": result.summary[
            "warning_checks"
        ],
        "failed_checks": result.summary[
            "failed_checks"
        ],
        "total_checks": result.summary[
            "total_checks"
        ],
        "checked_at": result.summary[
            "checked_at"
        ],
    }

def run_portfolio_pipeline(
    excel_path: Path = DEFAULT_EXCEL_PATH,
    replace_existing: bool = False,
    progress_callback: ProgressCallback | None = (
        console_progress_callback
    ),
) -> PortfolioPipelineResult:
    """
    Import the portfolio and rebuild all core portfolio data.
    """

    excel_path = (
        Path(excel_path)
        .expanduser()
        .resolve()
    )

    pipeline_started = perf_counter()
    stage_results: dict[str, Any] = {}

    stages: list[
        tuple[
            str,
            str,
            Callable[[], Any],
        ]
    ] = [
        (
            "database_setup",
            "Preparing database",
            create_database,
        ),
        (
            "excel_import",
            "Importing portfolio transactions",
            lambda: import_excel_to_database(
                excel_path=excel_path,
                replace_existing=replace_existing,
            ),
        ),
        (
            "asset_metadata",
            "Loading asset information",
            refresh_asset_metadata,
        ),
        (
            "market_data",
            "Downloading market prices",
            refresh_market_data_checked,
        ),
        (
            "holdings_summary",
            "Calculating current holdings",
            refresh_holdings_summary,
        ),
        (
            "portfolio_daily",
            "Building daily portfolio history",
            refresh_portfolio_daily_checked,
        ),
        (
            "security_daily",
            "Building daily security history",
            refresh_security_daily,
        ),
        (
            "validation",
            "Validating portfolio results",
            validate_final_portfolio,
        ),
    ]

    total_stages = len(stages)

    for (
        stage_index,
        (
            stage_key,
            stage_name,
            function,
        ),
    ) in enumerate(stages, start=1):

        stage_results[stage_key] = run_stage(
            stage_index=stage_index,
            total_stages=total_stages,
            stage_key=stage_key,
            stage_name=stage_name,
            function=function,
            callback=progress_callback,
        )

    elapsed_seconds = (
        perf_counter() - pipeline_started
    )

    return PortfolioPipelineResult(
        excel_path=excel_path,
        database_path=DATABASE_PATH,
        stage_results=stage_results,
        elapsed_seconds=elapsed_seconds,
    )


def run_selected_portfolio_source(
    source: str,
    excel_path: Path | None = None,
    midas_folder: Path = DEFAULT_MIDAS_FOLDER,
    midas_output: Path = DEFAULT_MIDAS_OUTPUT,
    sec_contact_email: str | None = None,
    approve_all: bool = False,
    reject_all: bool = False,
    overwrite_midas_output: bool = False,
    replace_existing: bool = False,
    progress_callback: ProgressCallback | None = (
        console_progress_callback
    ),
) -> PortfolioPipelineResult:
    """
    Build the portfolio from either an Excel workbook
    or Midas statement PDFs.
    """

    normalized_source = (
        str(source)
        .strip()
        .casefold()
    )

    if normalized_source not in {
        "excel",
        "midas",
    }:
        raise ValueError(
            "Source must be either 'excel' "
            "or 'midas'."
        )

    if normalized_source == "excel":
        selected_excel_path = (
            DEFAULT_EXCEL_PATH
            if excel_path is None
            else Path(excel_path)
        )

        print()
        print("Selected source: Excel")
        print(
            "Excel file: "
            f"{selected_excel_path}"
        )

    else:
        if excel_path is not None:
            raise ValueError(
                "Do not provide an Excel path when "
                "the selected source is Midas."
            )

        selected_midas_folder = (
            Path(midas_folder)
            .expanduser()
            .resolve()
        )

        selected_midas_output = (
            Path(midas_output)
            .expanduser()
            .resolve()
        )

        print()
        print("Selected source: Midas statements")
        print(
            "Midas folder: "
            f"{selected_midas_folder}"
        )

        run_midas_pipeline(
            folder=selected_midas_folder,
            output=selected_midas_output,
            sec_contact_email=(
                sec_contact_email
            ),
            approve_all=approve_all,
            reject_all=reject_all,
            overwrite=(
                overwrite_midas_output
            ),
        )

        selected_excel_path = (
            selected_midas_output
        )

        print()
        print(
            "Midas statements were converted "
            "to Excel successfully."
        )

    return run_portfolio_pipeline(
        excel_path=selected_excel_path,
        replace_existing=replace_existing,
        progress_callback=progress_callback,
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build command-line arguments.
    """

    parser = ArgumentParser(
        description=(
            "Build the portfolio from either an "
            "Excel workbook or Midas statements."
        )
    )

    parser.add_argument(
        "excel_path",
        nargs="?",
        type=Path,
        default=None,
        help=(
            "Portfolio Excel file. Used only when "
            "--source excel is selected. "
            "Default: portfolio.xlsx"
        ),
    )

    parser.add_argument(
        "--source",
        choices=[
            "excel",
            "midas",
        ],
        default="excel",
        help=(
            "Portfolio input type. "
            "Default: excel"
        ),
    )

    parser.add_argument(
        "--midas-folder",
        type=Path,
        default=DEFAULT_MIDAS_FOLDER,
        help=(
            "Folder containing Midas PDF statements. "
            "Default: midas_ekstreleri"
        ),
    )

    parser.add_argument(
        "--midas-output",
        type=Path,
        default=DEFAULT_MIDAS_OUTPUT,
        help=(
            "Excel file generated from Midas PDFs. "
            "Default: midas_portfolio_generated.xlsx"
        ),
    )

    parser.add_argument(
        "--sec-contact-email",
        help=(
            "Contact email used only for SEC "
            "fair-access identification. If omitted, "
            "the terminal asks for it."
        ),
    )

    decision_group = (
        parser.add_mutually_exclusive_group()
    )

    decision_group.add_argument(
        "--approve-all",
        action="store_true",
        help=(
            "Approve every proposed Midas "
            "corporate action."
        ),
    )

    decision_group.add_argument(
        "--reject-all",
        action="store_true",
        help=(
            "Reject every proposed Midas "
            "corporate action."
        ),
    )

    parser.add_argument(
        "--overwrite-midas-output",
        action="store_true",
        help=(
            "Allow replacement of an existing "
            "generated Midas Excel file."
        ),
    )

    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "Replace existing portfolio data. "
            "Use only when the selected source "
            "contains the complete portfolio history."
        ),
    )

    return parser


def main() -> None:
    """
    Run the complete portfolio workflow.
    """

    arguments = (
        build_argument_parser()
        .parse_args()
    )

    result = run_selected_portfolio_source(
        source=arguments.source,
        excel_path=arguments.excel_path,
        midas_folder=arguments.midas_folder,
        midas_output=arguments.midas_output,
        sec_contact_email=(
            arguments.sec_contact_email
        ),
        approve_all=arguments.approve_all,
        reject_all=arguments.reject_all,
        overwrite_midas_output=(
            arguments.overwrite_midas_output
        ),
        replace_existing=(
            arguments.replace_existing
        ),
    )

    print()
    print(
        "Portfolio pipeline completed successfully."
    )
    print(
        f"Database: {result.database_path}"
    )
    print(
        "Elapsed time: "
        f"{result.elapsed_seconds:.2f} seconds"
    )


if __name__ == "__main__":
    main()
