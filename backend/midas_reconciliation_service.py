from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pandas as pd

from midas_import_service import (
    CombinedMidasStatements,
    collect_pdf_paths,
    console_progress_callback,
    load_midas_statements,
)


RECONCILIATION_COLUMNS = [
    "previous_statement_date",
    "statement_date",
    "ticker",
    "previous_shares",
    "bought_shares",
    "sold_shares",
    "expected_shares",
    "observed_shares",
    "difference",
    "implied_ratio",
    "candidate_factor",
    "status",
    "candidate_action",
    "confidence",
]


@dataclass
class MidasReconciliationResult:
    """
    Result of comparing transactions with monthly Midas
    holding snapshots.
    """

    checks: pd.DataFrame
    summary: dict[str, Any]
    warnings: list[str]

    @property
    def issues(self) -> pd.DataFrame:
        """
        Return only rows requiring further analysis.
        """

        if self.checks.empty:
            return self.checks.copy()

        return self.checks.loc[
            self.checks["status"] != "MATCH"
        ].reset_index(drop=True)


def clean_share_value(
    value: float,
    tolerance: float,
) -> float:
    """
    Convert tiny floating-point balances to zero.
    """

    numeric_value = float(value)

    if abs(numeric_value) <= tolerance:
        return 0.0

    return numeric_value


def numbers_match(
    left: float,
    right: float,
    tolerance: float,
) -> bool:
    """
    Compare share quantities using absolute and relative
    tolerance.
    """

    difference = abs(left - right)

    allowed_difference = max(
        tolerance,
        tolerance
        * max(
            abs(left),
            abs(right),
            1.0,
        ),
    )

    return difference <= allowed_difference


def infer_candidate_factor(
    expected_shares: float,
    observed_shares: float,
    tolerance: float,
) -> tuple[float | None, float | None]:
    """
    Detect an exact-looking rational share ratio.

    This only creates a candidate. It does not apply a stock
    split automatically.
    """

    if (
        expected_shares <= tolerance
        or observed_shares <= tolerance
    ):
        return None, None

    implied_ratio = (
        observed_shares / expected_shares
    )

    if numbers_match(
        implied_ratio,
        1.0,
        tolerance,
    ):
        return implied_ratio, None

    rational = Fraction(
        implied_ratio
    ).limit_denominator(20)

    candidate_factor = (
        rational.numerator
        / rational.denominator
    )

    ratio_tolerance = max(
        tolerance,
        abs(implied_ratio) * 1e-6,
    )

    if (
        abs(
            candidate_factor - implied_ratio
        )
        <= ratio_tolerance
        and 0.01
        <= candidate_factor
        <= 100.0
    ):
        return (
            implied_ratio,
            candidate_factor,
        )

    return implied_ratio, None


def classify_reconciliation_row(
    previous_shares: float,
    bought_shares: float,
    sold_shares: float,
    expected_shares: float,
    observed_shares: float,
    tolerance: float,
) -> tuple[
    str,
    str,
    str,
    float | None,
    float | None,
]:
    """
    Classify one monthly ticker reconciliation.

    Returned values:
        status
        candidate_action
        confidence
        implied_ratio
        candidate_factor
    """

    (
        implied_ratio,
        candidate_factor,
    ) = infer_candidate_factor(
        expected_shares=expected_shares,
        observed_shares=observed_shares,
        tolerance=tolerance,
    )

    if numbers_match(
        expected_shares,
        observed_shares,
        tolerance,
    ):
        return (
            "MATCH",
            "NONE",
            "CONFIRMED",
            implied_ratio,
            None,
        )

    if expected_shares < -tolerance:
        return (
            "NEGATIVE_EXPECTED_POSITION",
            "MISSING_BUY_OR_CORPORATE_ACTION",
            "REVIEW_REQUIRED",
            implied_ratio,
            candidate_factor,
        )

    no_recorded_buys = (
        bought_shares <= tolerance
    )

    no_recorded_sells = (
        sold_shares <= tolerance
    )

    if (
        previous_shares <= tolerance
        and no_recorded_buys
        and observed_shares > tolerance
    ):
        return (
            "POSITION_APPEARED",
            (
                "POSSIBLE_TICKER_CHANGE_TRANSFER_"
                "OR_MISSING_BUY"
            ),
            "REVIEW_REQUIRED",
            implied_ratio,
            candidate_factor,
        )

    if (
        expected_shares > tolerance
        and observed_shares <= tolerance
    ):
        return (
            "POSITION_DISAPPEARED",
            (
                "POSSIBLE_CASH_ACQUISITION_"
                "TICKER_CHANGE_TRANSFER_"
                "OR_MISSING_SELL"
            ),
            "REVIEW_REQUIRED",
            implied_ratio,
            candidate_factor,
        )

    if candidate_factor is not None:
        no_period_trades = (
            no_recorded_buys
            and no_recorded_sells
        )

        return (
            "QUANTITY_MISMATCH",
            "POSSIBLE_STOCK_SPLIT",
            (
                "HIGH_CANDIDATE"
                if no_period_trades
                else "MEDIUM_CANDIDATE"
            ),
            implied_ratio,
            candidate_factor,
        )

    return (
        "QUANTITY_MISMATCH",
        "UNEXPLAINED_QUANTITY_CHANGE",
        "REVIEW_REQUIRED",
        implied_ratio,
        None,
    )


def prepare_trade_quantities(
    trades: pd.DataFrame,
) -> pd.DataFrame:
    """
    Prepare raw Buy and Sell share quantities.

    Raw PDF values are deliberately preserved. No split
    adjustment is performed here.
    """

    if trades.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "ticker",
                "bought_shares",
                "sold_shares",
            ]
        )

    prepared = trades[
        [
            "date",
            "ticker",
            "side",
            "shares",
        ]
    ].copy()

    prepared["date"] = pd.to_datetime(
        prepared["date"]
    ).dt.date

    prepared["ticker"] = (
        prepared["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    prepared["bought_shares"] = (
        prepared["shares"].where(
            prepared["side"] == "Buy",
            0.0,
        )
    )

    prepared["sold_shares"] = (
        prepared["shares"].where(
            prepared["side"] == "Sell",
            0.0,
        )
    )

    return (
        prepared.groupby(
            ["date", "ticker"],
            as_index=False,
            sort=True,
        )[
            [
                "bought_shares",
                "sold_shares",
            ]
        ]
        .sum()
    )


def prepare_holding_snapshots(
    holdings: pd.DataFrame,
) -> pd.DataFrame:
    """
    Prepare monthly Midas portfolio-summary balances.
    """

    if holdings.empty:
        raise ValueError(
            "No Midas holding snapshots were found. "
            "Monthly reconciliation cannot be performed."
        )

    prepared = holdings[
        [
            "statement_date",
            "ticker",
            "shares",
        ]
    ].copy()

    prepared["statement_date"] = (
        pd.to_datetime(
            prepared["statement_date"]
        ).dt.date
    )

    prepared["ticker"] = (
        prepared["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    duplicate_rows = prepared.duplicated(
        subset=[
            "statement_date",
            "ticker",
        ],
        keep=False,
    )

    if duplicate_rows.any():
        duplicate_values = (
            prepared.loc[
                duplicate_rows,
                [
                    "statement_date",
                    "ticker",
                ],
            ]
            .drop_duplicates()
            .astype(str)
            .agg(" / ".join, axis=1)
            .tolist()
        )

        raise ValueError(
            "Duplicate holding snapshots remain after "
            "statement loading: "
            + ", ".join(duplicate_values)
        )

    return prepared.sort_values(
        [
            "statement_date",
            "ticker",
        ],
        kind="stable",
    ).reset_index(drop=True)


def interval_trade_totals(
    prepared_trades: pd.DataFrame,
    previous_date: date | None,
    current_date: date,
) -> pd.DataFrame:
    """
    Calculate Buy and Sell quantities between two statement
    dates.
    """

    if prepared_trades.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "bought_shares",
                "sold_shares",
            ]
        )

    date_filter = (
        prepared_trades["date"]
        <= current_date
    )

    if previous_date is not None:
        date_filter &= (
            prepared_trades["date"]
            > previous_date
        )

    interval = prepared_trades.loc[
        date_filter
    ]

    if interval.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "bought_shares",
                "sold_shares",
            ]
        )

    return (
        interval.groupby(
            "ticker",
            as_index=False,
            sort=True,
        )[
            [
                "bought_shares",
                "sold_shares",
            ]
        ]
        .sum()
    )


def snapshot_share_map(
    snapshots: pd.DataFrame,
    statement_date: date,
) -> dict[str, float]:
    """
    Return ticker balances for one statement date.
    """

    rows = snapshots.loc[
        snapshots["statement_date"]
        == statement_date
    ]

    return {
        str(row["ticker"]): float(
            row["shares"]
        )
        for _, row in rows.iterrows()
    }


def trade_share_maps(
    totals: pd.DataFrame,
) -> tuple[
    dict[str, float],
    dict[str, float],
]:
    """
    Convert interval trade totals to ticker dictionaries.
    """

    bought = {
        str(row["ticker"]): float(
            row["bought_shares"]
        )
        for _, row in totals.iterrows()
    }

    sold = {
        str(row["ticker"]): float(
            row["sold_shares"]
        )
        for _, row in totals.iterrows()
    }

    return bought, sold


def reconcile_midas_holdings(
    statements: CombinedMidasStatements,
    tolerance: float = 1e-8,
) -> MidasReconciliationResult:
    """
    Compare transaction quantities with monthly Midas
    holding snapshots.

    Every period starts from the previous observed Midas
    balance. This prevents one unresolved month from
    contaminating every later month.
    """

    if tolerance <= 0:
        raise ValueError(
            "Reconciliation tolerance must be positive."
        )

    prepared_trades = prepare_trade_quantities(
        statements.trades
    )

    snapshots = prepare_holding_snapshots(
        statements.holdings
    )

    statement_dates = sorted(
        snapshots["statement_date"]
        .drop_duplicates()
        .tolist()
    )

    if not statement_dates:
        raise ValueError(
            "No statement dates are available."
        )

    checks: list[dict[str, Any]] = []

    previous_date: date | None = None
    previous_holdings: dict[str, float] = {}

    for statement_date in statement_dates:
        observed_holdings = snapshot_share_map(
            snapshots,
            statement_date,
        )

        interval_totals = interval_trade_totals(
            prepared_trades=prepared_trades,
            previous_date=previous_date,
            current_date=statement_date,
        )

        (
            bought_shares_by_ticker,
            sold_shares_by_ticker,
        ) = trade_share_maps(interval_totals)

        relevant_tickers = sorted(
            set(previous_holdings)
            | set(observed_holdings)
            | set(bought_shares_by_ticker)
            | set(sold_shares_by_ticker)
        )

        for ticker in relevant_tickers:
            previous_shares = clean_share_value(
                previous_holdings.get(
                    ticker,
                    0.0,
                ),
                tolerance,
            )

            bought_shares = clean_share_value(
                bought_shares_by_ticker.get(
                    ticker,
                    0.0,
                ),
                tolerance,
            )

            sold_shares = clean_share_value(
                sold_shares_by_ticker.get(
                    ticker,
                    0.0,
                ),
                tolerance,
            )

            expected_shares = clean_share_value(
                previous_shares
                + bought_shares
                - sold_shares,
                tolerance,
            )

            observed_shares = clean_share_value(
                observed_holdings.get(
                    ticker,
                    0.0,
                ),
                tolerance,
            )

            difference = clean_share_value(
                observed_shares
                - expected_shares,
                tolerance,
            )

            (
                status,
                candidate_action,
                confidence,
                implied_ratio,
                candidate_factor,
            ) = classify_reconciliation_row(
                previous_shares=previous_shares,
                bought_shares=bought_shares,
                sold_shares=sold_shares,
                expected_shares=expected_shares,
                observed_shares=observed_shares,
                tolerance=tolerance,
            )

            checks.append({
                "previous_statement_date": (
                    previous_date
                ),
                "statement_date": (
                    statement_date
                ),
                "ticker": ticker,
                "previous_shares": (
                    previous_shares
                ),
                "bought_shares": bought_shares,
                "sold_shares": sold_shares,
                "expected_shares": (
                    expected_shares
                ),
                "observed_shares": (
                    observed_shares
                ),
                "difference": difference,
                "implied_ratio": (
                    implied_ratio
                ),
                "candidate_factor": (
                    candidate_factor
                ),
                "status": status,
                "candidate_action": (
                    candidate_action
                ),
                "confidence": confidence,
            })

        previous_holdings = observed_holdings
        previous_date = statement_date

    checks_frame = pd.DataFrame(
        checks,
        columns=RECONCILIATION_COLUMNS,
    )

    status_counts = (
        checks_frame["status"]
        .value_counts()
        .to_dict()
        if not checks_frame.empty
        else {}
    )

    issue_count = int(
        (
            checks_frame["status"]
            != "MATCH"
        ).sum()
    )

    candidate_split_count = int(
        (
            checks_frame["candidate_action"]
            == "POSSIBLE_STOCK_SPLIT"
        ).sum()
    )

    appeared_count = int(
        (
            checks_frame["status"]
            == "POSITION_APPEARED"
        ).sum()
    )

    disappeared_count = int(
        (
            checks_frame["status"]
            == "POSITION_DISAPPEARED"
        ).sum()
    )

    warnings = statements.warnings.copy()

    if issue_count:
        warnings.append(
            f"{issue_count} holding reconciliation "
            "row requires corporate-action analysis."
        )

    summary = {
        "status": (
            "REVIEW_REQUIRED"
            if issue_count
            else "RECONCILED"
        ),
        "statement_date_count": len(
            statement_dates
        ),
        "check_count": len(checks_frame),
        "matched_count": int(
            (
                checks_frame["status"]
                == "MATCH"
            ).sum()
        ),
        "issue_count": issue_count,
        "candidate_split_count": (
            candidate_split_count
        ),
        "appeared_position_count": (
            appeared_count
        ),
        "disappeared_position_count": (
            disappeared_count
        ),
        "status_counts": status_counts,
        "first_statement_date": (
            statement_dates[0].isoformat()
        ),
        "last_statement_date": (
            statement_dates[-1].isoformat()
        ),
    }

    return MidasReconciliationResult(
        checks=checks_frame,
        summary=summary,
        warnings=list(
            dict.fromkeys(warnings)
        ),
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build the reconciliation test command.
    """

    parser = ArgumentParser(
        description=(
            "Compare Midas PDF transactions with "
            "monthly holding snapshots."
        )
    )

    parser.add_argument(
        "--folder",
        type=Path,
        required=True,
        help=(
            "Folder containing Midas PDF statements."
        ),
    )

    return parser


def main() -> None:
    """
    Run reconciliation without changing the database.
    """

    arguments = build_argument_parser().parse_args()

    pdf_paths = collect_pdf_paths(
        arguments.folder
    )

    statements = load_midas_statements(
        sources=pdf_paths,
        progress_callback=(
            console_progress_callback
        ),
    )

    result = reconcile_midas_holdings(
        statements
    )

    print()
    print("Midas holding reconciliation completed.")
    print()

    for key, value in result.summary.items():
        print(f"{key}: {value}")

    if not result.issues.empty:
        print()
        print("RECONCILIATION ISSUES")
        print()

        display_columns = [
            "previous_statement_date",
            "statement_date",
            "ticker",
            "previous_shares",
            "bought_shares",
            "sold_shares",
            "expected_shares",
            "observed_shares",
            "candidate_factor",
            "status",
            "candidate_action",
            "confidence",
        ]

        print(
            result.issues[
                display_columns
            ].to_string(index=False)
        )


if __name__ == "__main__":
    main()
