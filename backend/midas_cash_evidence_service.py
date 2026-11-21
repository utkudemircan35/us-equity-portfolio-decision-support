from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from midas_corporate_action_resolver import (
    CorporateActionResolutionResult,
    resolve_corporate_actions,
)
from midas_import_service import (
    CombinedMidasStatements,
    collect_pdf_paths,
    console_progress_callback,
    load_midas_statements,
)
from midas_reconciliation_service import (
    reconcile_midas_holdings,
)


CASH_EVIDENCE_COLUMNS = [
    "ticker",
    "disappearance_statement_date",
    "cash_movement_date",
    "cash_movement_time",
    "cash_movement_type",
    "cash_amount_usd",
    "missing_shares",
    "implied_price_per_share",
    "previous_market_value_usd",
    "cash_to_market_value_ratio",
    "day_distance",
    "evidence_score",
    "confidence",
    "description",
    "source_file",
]


@dataclass
class CashAcquisitionEvidenceResult:
    """
    Cash movements that may support unresolved position
    disappearances.
    """

    evidence: pd.DataFrame
    best_candidates: pd.DataFrame
    summary: dict[str, Any]
    warnings: list[str]


def prepare_cash_movements(
    statements: CombinedMidasStatements,
) -> pd.DataFrame:
    """
    Normalize parsed cash movements for evidence matching.
    """

    if statements.cash_movements.empty:
        return pd.DataFrame(
            columns=statements.cash_movements.columns
        )

    cash_movements = (
        statements.cash_movements.copy()
    )

    cash_movements["date"] = pd.to_datetime(
        cash_movements["date"]
    ).dt.date

    cash_movements["type"] = (
        cash_movements["type"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    return cash_movements


def previous_holding_values(
    statements: CombinedMidasStatements,
    ticker: str,
    statement_date: date,
) -> tuple[
    float | None,
    float | None,
]:
    """
    Return the latest market value and average cost before
    a position disappeared.
    """

    if statements.holdings.empty:
        return None, None

    holdings = statements.holdings.copy()

    holdings["statement_date"] = pd.to_datetime(
        holdings["statement_date"]
    ).dt.date

    rows = holdings.loc[
        (
            holdings["ticker"]
            .astype(str)
            .str.upper()
            == ticker
        )
        & (
            holdings["statement_date"]
            < statement_date
        )
    ].sort_values(
        "statement_date",
        kind="stable",
    )

    if rows.empty:
        return None, None

    latest = rows.iloc[-1]

    market_value = latest.get(
        "market_value_usd"
    )

    average_cost = latest.get(
        "average_cost_usd"
    )

    return (
        float(market_value)
        if pd.notna(market_value)
        else None,
        float(average_cost)
        if pd.notna(average_cost)
        else None,
    )


def calculate_evidence_score(
    cash_type: str,
    cash_amount: float,
    previous_market_value: float | None,
    day_distance: int,
) -> float:
    """
    Rank possible cash-settlement evidence.

    This score only ranks candidates. It never determines
    the official acquisition price.
    """

    score = 0.0

    # Movements near the month-end disappearance receive
    # a higher score.
    score += max(
        0.0,
        40.0 - min(abs(day_distance), 40),
    )

    if cash_type == "WITHDRAWAL":
        # Midas may show a later USD-to-TRY conversion as
        # a withdrawal from the USD portfolio.
        score += 20.0

    elif cash_type == "DEPOSIT":
        score += 15.0

    if (
        previous_market_value is not None
        and previous_market_value > 0
    ):
        value_ratio = (
            cash_amount
            / previous_market_value
        )

        if 0.75 <= value_ratio <= 1.50:
            score += 40.0

        elif 0.50 <= value_ratio <= 2.00:
            score += 30.0

        elif 0.25 <= value_ratio <= 4.00:
            score += 15.0

    return score


def confidence_from_score(
    score: float,
) -> str:
    """
    Convert an evidence score to a review category.
    """

    if score >= 75:
        return "HIGH_CANDIDATE"

    if score >= 50:
        return "MEDIUM_CANDIDATE"

    return "LOW_CANDIDATE"


def find_cash_evidence(
    statements: CombinedMidasStatements,
    corporate_actions: CorporateActionResolutionResult,
    days_before_statement: int = 35,
    days_after_statement: int = 14,
    tolerance: float = 1e-8,
) -> CashAcquisitionEvidenceResult:
    """
    Find cash movements near unresolved disappearing
    positions.

    This function does not create a synthetic Sell trade.
    """

    if days_before_statement < 0:
        raise ValueError(
            "days_before_statement cannot be negative."
        )

    if days_after_statement < 0:
        raise ValueError(
            "days_after_statement cannot be negative."
        )

    cash_movements = prepare_cash_movements(
        statements
    )

    evidence_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    unresolved = (
        corporate_actions.unresolved_issues
    )

    for _, issue in unresolved.iterrows():
        ticker = str(
            issue["ticker"]
        ).upper()

        previous_shares = float(
            issue["previous_shares"]
        )

        observed_shares = float(
            issue["observed_shares"]
        )

        difference = float(
            issue[
                "split_adjusted_difference"
            ]
        )

        if (
            previous_shares <= tolerance
            or observed_shares > tolerance
            or difference >= -tolerance
        ):
            continue

        statement_date = issue[
            "statement_date"
        ]

        missing_shares = abs(difference)

        (
            previous_market_value,
            previous_average_cost,
        ) = previous_holding_values(
            statements=statements,
            ticker=ticker,
            statement_date=statement_date,
        )

        window_start = (
            statement_date
            - timedelta(
                days=days_before_statement
            )
        )

        window_end = (
            statement_date
            + timedelta(
                days=days_after_statement
            )
        )

        nearby_cash = cash_movements.loc[
            (
                cash_movements["date"]
                >= window_start
            )
            & (
                cash_movements["date"]
                <= window_end
            )
            & (
                cash_movements["type"].isin(
                    [
                        "DEPOSIT",
                        "WITHDRAWAL",
                    ]
                )
            )
        ]

        if nearby_cash.empty:
            warnings.append(
                f"No nearby cash movement was found "
                f"for disappearing position {ticker}."
            )

            continue

        for _, cash_row in nearby_cash.iterrows():
            cash_amount = float(
                cash_row["amount_usd"]
            )

            implied_price = (
                cash_amount / missing_shares
            )

            day_distance = (
                cash_row["date"]
                - statement_date
            ).days

            score = calculate_evidence_score(
                cash_type=str(
                    cash_row["type"]
                ),
                cash_amount=cash_amount,
                previous_market_value=(
                    previous_market_value
                ),
                day_distance=day_distance,
            )

            value_ratio = None

            if (
                previous_market_value is not None
                and previous_market_value > 0
            ):
                value_ratio = (
                    cash_amount
                    / previous_market_value
                )

            description = cash_row.get(
                "description"
            )

            if pd.isna(description):
                description = None

            evidence_rows.append({
                "ticker": ticker,
                "disappearance_statement_date": (
                    statement_date
                ),
                "cash_movement_date": (
                    cash_row["date"]
                ),
                "cash_movement_time": (
                    cash_row["time"]
                ),
                "cash_movement_type": (
                    cash_row["type"]
                ),
                "cash_amount_usd": cash_amount,
                "missing_shares": missing_shares,
                "implied_price_per_share": (
                    implied_price
                ),
                "previous_market_value_usd": (
                    previous_market_value
                ),
                "cash_to_market_value_ratio": (
                    value_ratio
                ),
                "day_distance": day_distance,
                "evidence_score": score,
                "confidence": (
                    confidence_from_score(
                        score
                    )
                ),
                "description": description,
                "source_file": cash_row[
                    "source_file"
                ],
            })

    evidence = pd.DataFrame(
        evidence_rows,
        columns=CASH_EVIDENCE_COLUMNS,
    )

    if evidence.empty:
        best_candidates = evidence.copy()

    else:
        evidence = evidence.sort_values(
            [
                "ticker",
                "evidence_score",
                "cash_movement_date",
            ],
            ascending=[
                True,
                False,
                True,
            ],
            kind="stable",
        ).reset_index(drop=True)

        best_candidates = (
            evidence.groupby(
                "ticker",
                sort=False,
                as_index=False,
            )
            .head(1)
            .reset_index(drop=True)
        )

    high_candidate_count = int(
        (
            best_candidates["confidence"]
            == "HIGH_CANDIDATE"
        ).sum()
    ) if not best_candidates.empty else 0

    summary = {
        "status": (
            "EVIDENCE_FOUND"
            if not best_candidates.empty
            else "NO_EVIDENCE"
        ),
        "unresolved_position_count": len(
            unresolved
        ),
        "cash_evidence_count": len(evidence),
        "best_candidate_count": len(
            best_candidates
        ),
        "high_candidate_count": (
            high_candidate_count
        ),
    }

    return CashAcquisitionEvidenceResult(
        evidence=evidence,
        best_candidates=best_candidates,
        summary=summary,
        warnings=list(dict.fromkeys(warnings)),
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build terminal-test arguments.
    """

    parser = ArgumentParser(
        description=(
            "Find Midas cash evidence for unresolved "
            "position disappearances."
        )
    )

    parser.add_argument(
        "--folder",
        type=Path,
        required=True,
        help="Folder containing Midas PDFs.",
    )

    return parser


def main() -> None:
    """
    Find cash evidence without changing the database.
    """

    arguments = build_argument_parser().parse_args()

    statements = load_midas_statements(
        sources=collect_pdf_paths(
            arguments.folder
        ),
        progress_callback=(
            console_progress_callback
        ),
    )

    reconciliation = reconcile_midas_holdings(
        statements
    )

    corporate_actions = resolve_corporate_actions(
        statements=statements,
        reconciliation=reconciliation,
    )

    result = find_cash_evidence(
        statements=statements,
        corporate_actions=corporate_actions,
    )

    print()
    print("Cash-acquisition evidence search completed.")
    print()

    for key, value in result.summary.items():
        print(f"{key}: {value}")

    if not result.best_candidates.empty:
        print()
        print("BEST CASH EVIDENCE")
        print()

        print(
            result.best_candidates[
                [
                    "ticker",
                    "cash_movement_date",
                    "cash_movement_type",
                    "cash_amount_usd",
                    "missing_shares",
                    "implied_price_per_share",
                    "day_distance",
                    "evidence_score",
                    "confidence",
                    "description",
                    "source_file",
                ]
            ].to_string(index=False)
        )


if __name__ == "__main__":
    main()
