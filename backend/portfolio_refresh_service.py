from dataclasses import dataclass
from time import perf_counter
from typing import Any

from asset_metadata import (
    refresh_asset_metadata,
)
from database import (
    DATABASE_PATH,
    validate_database,
)
from market_data import (
    build_download_requests,
    download_all_market_prices,
    save_market_prices,
)
from portfolio_calculator import (
    refresh_holdings_summary,
)
from portfolio_daily_calculator import (
    refresh_portfolio_daily,
)
from portfolio_pipeline import (
    PipelineProgress,
    ProgressCallback,
    console_progress_callback,
    run_stage,
)
from security_daily_calculator import (
    refresh_security_daily,
)
from portfolio_validation_service import (
    run_portfolio_validation,
)



@dataclass
class PortfolioRefreshResult:
    database_path: str
    stage_results: dict[str, Any]
    elapsed_seconds: float


def refresh_market_data_safely() -> dict[str, Any]:
    """
    Download all required market prices before replacing
    the existing market-price table.

    Existing prices are not deleted when a required
    download fails.
    """

    requests = build_download_requests(
        include_benchmarks=True
    )

    (
        prices,
        trade_fallback_tickers,
        failed_required_tickers,
    ) = download_all_market_prices(
        requests
    )

    if failed_required_tickers:
        details = "; ".join(
            f"{ticker}: {reason}"
            for ticker, reason
            in failed_required_tickers.items()
        )

        raise RuntimeError(
            "Required market-price downloads "
            "failed: "
            + details
        )

    saved_rows = save_market_prices(
        prices=prices,
        clear_existing=True,
    )

    open_tickers = sorted(
        request["ticker"]
        for request in requests
        if (
            request["instrument_role"]
            == "OPEN"
        )
    )

    closed_tickers = sorted(
        request["ticker"]
        for request in requests
        if (
            request["instrument_role"]
            == "CLOSED"
        )
    )

    benchmark_tickers = sorted(
        request["ticker"]
        for request in requests
        if (
            request["instrument_role"]
            == "BENCHMARK"
        )
    )

    return {
        "saved_rows": saved_rows,
        "open_tickers": open_tickers,
        "closed_tickers": closed_tickers,
        "benchmark_tickers": (
            benchmark_tickers
        ),
        "trade_fallback_tickers": (
            trade_fallback_tickers
        ),
        "failed_required_tickers": {},
    }


def refresh_portfolio_daily_checked() -> dict[str, Any]:
    """
    Refresh portfolio_daily and require a successful
    reconciliation with holdings_summary.
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


def validate_refreshed_portfolio(
) -> dict[str, Any]:
    """
    Validate the refreshed portfolio data.
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

def run_portfolio_refresh(
    progress_callback: (
        ProgressCallback | None
    ) = console_progress_callback,
) -> PortfolioRefreshResult:
    """
    Rebuild all calculated portfolio data without
    importing the Excel workbook.
    """

    validate_database()

    refresh_started = perf_counter()

    stage_results: dict[
        str,
        Any,
    ] = {}

    stages = [
        (
            "asset_metadata",
            "Refreshing asset information",
            refresh_asset_metadata,
        ),
        (
            "market_data",
            "Refreshing market prices",
            refresh_market_data_safely,
        ),
        (
            "holdings_summary",
            "Recalculating current holdings",
            refresh_holdings_summary,
        ),
        (
            "portfolio_daily",
            "Rebuilding daily portfolio history",
            refresh_portfolio_daily_checked,
        ),
        (
            "security_daily",
            "Rebuilding daily security history",
            refresh_security_daily,
        ),
        (
            "validation",
            "Validating refreshed portfolio",
            validate_refreshed_portfolio,
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
    ) in enumerate(
        stages,
        start=1,
    ):
        stage_results[stage_key] = (
            run_stage(
                stage_index=stage_index,
                total_stages=(
                    total_stages
                ),
                stage_key=stage_key,
                stage_name=stage_name,
                function=function,
                callback=(
                    progress_callback
                ),
            )
        )

    elapsed_seconds = (
        perf_counter()
        - refresh_started
    )

    return PortfolioRefreshResult(
        database_path=str(
            DATABASE_PATH
        ),
        stage_results=stage_results,
        elapsed_seconds=(
            elapsed_seconds
        ),
    )


def main() -> None:
    """
    Rebuild calculated portfolio data.
    """

    result = run_portfolio_refresh()

    print()
    print(
        "Portfolio refresh completed "
        "successfully."
    )

    print(
        f"Database: "
        f"{result.database_path}"
    )

    print(
        "Elapsed time: "
        f"{result.elapsed_seconds:.2f} "
        "seconds"
    )


if __name__ == "__main__":
    main()
