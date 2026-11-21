from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
import re

import pandas as pd
import yfinance as yf

from midas_import_service import (
    CombinedMidasStatements,
    collect_pdf_paths,
    console_progress_callback,
    load_midas_statements,
)
from midas_reconciliation_service import (
    MidasReconciliationResult,
    numbers_match,
    prepare_holding_snapshots,
    prepare_trade_quantities,
    reconcile_midas_holdings,
)


RESOLUTION_COLUMNS = [
    "previous_statement_date",
    "statement_date",
    "ticker",
    "previous_shares",
    "bought_shares",
    "sold_shares",
    "expected_shares",
    "split_adjusted_expected_shares",
    "observed_shares",
    "split_adjusted_difference",
    "split_events",
    "resolution_status",
    "resolved_action",
    "resolved_ticker",
    "confidence",
    "details",
]

TICKER_ALIAS_COLUMNS = [
    "old_ticker",
    "new_ticker",
    "detected_date",
    "carried_shares",
    "old_company_name",
    "new_company_name",
    "company_name_similarity",
    "confidence",
    "auto_apply",
]

SPLIT_EVENT_COLUMNS = [
    "ticker",
    "split_date",
    "split_ratio",
    "verified_by_holdings",
]


@dataclass
class CorporateActionResolutionResult:
    """
    Result of resolving reconciliation differences.
    """

    checks: pd.DataFrame
    ticker_aliases: pd.DataFrame
    split_events: pd.DataFrame
    unresolved_issues: pd.DataFrame
    split_lookup_errors: dict[str, str]
    summary: dict[str, Any]
    warnings: list[str]


def download_split_history(
    tickers: list[str],
) -> tuple[
    dict[str, list[tuple[date, float]]],
    dict[str, str],
]:
    """
    Download split events while preserving lookup failures.

    An unavailable ticker does not stop analysis of the
    remaining portfolio.
    """

    split_history: dict[
        str,
        list[tuple[date, float]],
    ] = {}

    lookup_errors: dict[str, str] = {}

    for ticker in sorted(set(tickers)):
        print(
            f"Checking corporate-action history: "
            f"{ticker}"
        )

        try:
            splits = (
                yf.Ticker(ticker)
                .get_splits(period="max")
            )

        except Exception as error:
            split_history[ticker] = []
            lookup_errors[ticker] = str(error)
            continue

        if splits is None:
            split_history[ticker] = []
            lookup_errors[ticker] = (
                "Yahoo Finance returned no valid "
                "split response."
            )
            continue

        ticker_events: list[
            tuple[date, float]
        ] = []

        for split_datetime, split_ratio in (
            splits.items()
        ):
            split_timestamp = pd.Timestamp(
                split_datetime
            )

            if split_timestamp.tzinfo is not None:
                split_timestamp = (
                    split_timestamp.tz_localize(
                        None
                    )
                )

            ratio = float(split_ratio)

            if ratio <= 0 or ratio == 1.0:
                continue

            ticker_events.append(
                (
                    split_timestamp.date(),
                    ratio,
                )
            )

        ticker_events.sort(
            key=lambda item: item[0]
        )

        split_history[ticker] = ticker_events

    return split_history, lookup_errors


def interval_split_events(
    ticker: str,
    previous_date: date | None,
    statement_date: date,
    split_history: dict[
        str,
        list[tuple[date, float]],
    ],
) -> list[tuple[date, float]]:
    """
    Return split events inside one statement interval.
    """

    events: list[tuple[date, float]] = []

    for split_date, ratio in split_history.get(
        ticker,
        [],
    ):
        if split_date > statement_date:
            continue

        if (
            previous_date is not None
            and split_date <= previous_date
        ):
            continue

        events.append(
            (split_date, ratio)
        )

    return events


def interval_ticker_trades(
    prepared_trades: pd.DataFrame,
    ticker: str,
    previous_date: date | None,
    statement_date: date,
) -> pd.DataFrame:
    """
    Return one ticker's transactions inside an interval.
    """

    if prepared_trades.empty:
        return prepared_trades.copy()

    date_filter = (
        prepared_trades["date"]
        <= statement_date
    )

    if previous_date is not None:
        date_filter &= (
            prepared_trades["date"]
            > previous_date
        )

    return prepared_trades.loc[
        date_filter
        & (
            prepared_trades["ticker"]
            == ticker
        )
    ].copy()


def simulate_split_adjusted_position(
    previous_shares: float,
    interval_trades: pd.DataFrame,
    split_events: list[
        tuple[date, float]
    ],
) -> float:
    """
    Replay splits and transactions chronologically.

    A split is applied before transactions on its effective
    date because trades on that date are already expressed
    in post-split units.
    """

    position = float(previous_shares)

    trades_by_date: dict[
        date,
        tuple[float, float],
    ] = {}

    for _, row in interval_trades.iterrows():
        trade_date = row["date"]

        bought, sold = trades_by_date.get(
            trade_date,
            (0.0, 0.0),
        )

        trades_by_date[trade_date] = (
            bought
            + float(row["bought_shares"]),
            sold
            + float(row["sold_shares"]),
        )

    splits_by_date: dict[
        date,
        list[float],
    ] = {}

    for split_date, ratio in split_events:
        splits_by_date.setdefault(
            split_date,
            [],
        ).append(float(ratio))

    event_dates = sorted(
        set(trades_by_date)
        | set(splits_by_date)
    )

    for event_date in event_dates:
        for ratio in splits_by_date.get(
            event_date,
            [],
        ):
            position *= ratio

        bought, sold = trades_by_date.get(
            event_date,
            (0.0, 0.0),
        )

        position += bought
        position -= sold

    return position


def format_split_events(
    events: list[tuple[date, float]],
) -> str:
    """
    Format split events for reports.
    """

    return ", ".join(
        f"{split_date.isoformat()} ({ratio}:1)"
        for split_date, ratio in events
    )


def build_split_adjusted_checks(
    statements: CombinedMidasStatements,
    reconciliation: MidasReconciliationResult,
    split_history: dict[
        str,
        list[tuple[date, float]],
    ],
    tolerance: float,
) -> pd.DataFrame:
    """
    Recalculate reconciliation issues using dated split
    events and transaction ordering.
    """

    prepared_trades = prepare_trade_quantities(
        statements.trades
    )

    rows: list[dict[str, Any]] = []

    for _, issue in reconciliation.issues.iterrows():
        ticker = str(issue["ticker"])

        previous_date = issue[
            "previous_statement_date"
        ]

        statement_date = issue[
            "statement_date"
        ]

        previous_shares = float(
            issue["previous_shares"]
        )

        interval_trades = interval_ticker_trades(
            prepared_trades=prepared_trades,
            ticker=ticker,
            previous_date=previous_date,
            statement_date=statement_date,
        )

        events = interval_split_events(
            ticker=ticker,
            previous_date=previous_date,
            statement_date=statement_date,
            split_history=split_history,
        )

        adjusted_expected = (
            simulate_split_adjusted_position(
                previous_shares=previous_shares,
                interval_trades=interval_trades,
                split_events=events,
            )
        )

        observed_shares = float(
            issue["observed_shares"]
        )

        adjusted_difference = (
            observed_shares
            - adjusted_expected
        )

        if abs(adjusted_difference) <= tolerance:
            adjusted_difference = 0.0

        if events and numbers_match(
            adjusted_expected,
            observed_shares,
            tolerance,
        ):
            resolution_status = "RESOLVED"
            resolved_action = "STOCK_SPLIT"
            confidence = "VERIFIED"
            details = (
                "Dated split history and raw trades "
                "reconcile exactly with the Midas "
                "holding snapshot."
            )

        else:
            resolution_status = "UNRESOLVED"
            resolved_action = None
            confidence = "REVIEW_REQUIRED"
            details = (
                "No verified split sequence reconciled "
                "this holding difference."
            )

        rows.append({
            "previous_statement_date": (
                previous_date
            ),
            "statement_date": statement_date,
            "ticker": ticker,
            "previous_shares": previous_shares,
            "bought_shares": float(
                issue["bought_shares"]
            ),
            "sold_shares": float(
                issue["sold_shares"]
            ),
            "expected_shares": float(
                issue["expected_shares"]
            ),
            "split_adjusted_expected_shares": (
                adjusted_expected
            ),
            "observed_shares": observed_shares,
            "split_adjusted_difference": (
                adjusted_difference
            ),
            "split_events": (
                format_split_events(events)
            ),
            "resolution_status": (
                resolution_status
            ),
            "resolved_action": resolved_action,
            "resolved_ticker": None,
            "confidence": confidence,
            "details": details,
        })

    return pd.DataFrame(
        rows,
        columns=RESOLUTION_COLUMNS,
    )


def resolve_timing_differences(
    checks: pd.DataFrame,
    tolerance: float,
) -> None:
    """
    Resolve a transaction that appears one statement later.

    Example:
        April difference: -0.95
        May difference:   +0.95
    """

    unresolved = checks.loc[
        checks["resolution_status"]
        == "UNRESOLVED"
    ]

    for ticker, group in unresolved.groupby(
        "ticker",
        sort=False,
    ):
        ordered = group.sort_values(
            "statement_date",
            kind="stable",
        )

        indices = ordered.index.tolist()

        for first_index, second_index in zip(
            indices,
            indices[1:],
        ):
            if (
                checks.at[
                    first_index,
                    "resolution_status",
                ]
                != "UNRESOLVED"
                or checks.at[
                    second_index,
                    "resolution_status",
                ]
                != "UNRESOLVED"
            ):
                continue

            first_difference = float(
                checks.at[
                    first_index,
                    "split_adjusted_difference",
                ]
            )

            second_difference = float(
                checks.at[
                    second_index,
                    "split_adjusted_difference",
                ]
            )

            first_date = checks.at[
                first_index,
                "statement_date",
            ]

            second_previous_date = checks.at[
                second_index,
                "previous_statement_date",
            ]

            if second_previous_date != first_date:
                continue

            if (
                first_difference >= -tolerance
                or second_difference <= tolerance
            ):
                continue

            if not numbers_match(
                abs(first_difference),
                second_difference,
                tolerance,
            ):
                continue

            detail = (
                "The missing quantity in one statement "
                "appears unchanged in the immediately "
                "following statement."
            )

            for index in [
                first_index,
                second_index,
            ]:
                checks.at[
                    index,
                    "resolution_status",
                ] = "RESOLVED"

                checks.at[
                    index,
                    "resolved_action",
                ] = "STATEMENT_TIMING_DIFFERENCE"

                checks.at[
                    index,
                    "confidence",
                ] = "VERIFIED"

                checks.at[
                    index,
                    "details",
                ] = detail


COMPANY_STOP_WORDS = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "class",
    "holding",
    "holdings",
    "group",
    "global",
    "limited",
    "ltd",
    "plc",
    "the",
}


def company_tokens(
    company_name: str | None,
) -> set[str]:
    """
    Extract meaningful company-name tokens.
    """

    if not company_name:
        return set()

    tokens = {
        token
        for token in re.findall(
            r"[a-z0-9]+",
            company_name.casefold(),
        )
        if (
            len(token) >= 3
            and token not in COMPANY_STOP_WORDS
        )
    }

    return tokens


def company_name_similarity(
    first_name: str | None,
    second_name: str | None,
) -> float:
    """
    Compare company identities without requiring exact
    display names.
    """

    first_tokens = company_tokens(first_name)
    second_tokens = company_tokens(second_name)

    if not first_tokens or not second_tokens:
        return 0.0

    shared = first_tokens & second_tokens

    return (
        len(shared)
        / min(
            len(first_tokens),
            len(second_tokens),
        )
    )


def latest_company_name(
    holdings: pd.DataFrame,
    ticker: str,
    statement_date: date,
) -> str | None:
    """
    Find the latest available company name for a ticker.
    """

    rows = holdings.loc[
        (
            holdings["ticker"]
            == ticker
        )
        & (
            holdings["statement_date"]
            <= statement_date
        )
    ].sort_values(
        "statement_date",
        kind="stable",
    )

    if rows.empty:
        return None

    value = rows.iloc[-1].get(
        "company_name"
    )

    if pd.isna(value):
        return None

    return str(value)


def resolve_ticker_changes(
    checks: pd.DataFrame,
    statements: CombinedMidasStatements,
    tolerance: float,
    maximum_day_gap: int = 62,
) -> pd.DataFrame:
    """
    Match a disappearing position with an equal unexplained
    quantity appearing under another ticker.
    """

    holdings = prepare_holding_snapshots(
        statements.holdings
    )

    company_names = (
        statements.holdings[
            [
                "statement_date",
                "ticker",
                "company_name",
            ]
        ]
        .copy()
    )

    company_names["statement_date"] = (
        pd.to_datetime(
            company_names["statement_date"]
        ).dt.date
    )

    company_names["ticker"] = (
        company_names["ticker"]
        .astype(str)
        .str.upper()
    )

    del holdings

    unresolved = checks.loc[
        checks["resolution_status"]
        == "UNRESOLVED"
    ]

    negative_rows = unresolved.loc[
        unresolved[
            "split_adjusted_difference"
        ]
        < -tolerance
    ]

    positive_rows = unresolved.loc[
        unresolved[
            "split_adjusted_difference"
        ]
        > tolerance
    ]

    candidate_pairs: list[
        dict[str, Any]
    ] = []

    for old_index, old_row in (
        negative_rows.iterrows()
    ):
        missing_shares = abs(
            float(
                old_row[
                    "split_adjusted_difference"
                ]
            )
        )

        for new_index, new_row in (
            positive_rows.iterrows()
        ):
            old_ticker = str(
                old_row["ticker"]
            )

            new_ticker = str(
                new_row["ticker"]
            )

            if old_ticker == new_ticker:
                continue

            old_date = old_row[
                "statement_date"
            ]

            new_date = new_row[
                "statement_date"
            ]

            day_gap = (
                new_date - old_date
            ).days

            if (
                day_gap < 0
                or day_gap > maximum_day_gap
            ):
                continue

            excess_shares = float(
                new_row[
                    "split_adjusted_difference"
                ]
            )

            if not numbers_match(
                missing_shares,
                excess_shares,
                tolerance,
            ):
                continue

            old_company_name = (
                latest_company_name(
                    holdings=company_names,
                    ticker=old_ticker,
                    statement_date=old_date,
                )
            )

            new_company_name = (
                latest_company_name(
                    holdings=company_names,
                    ticker=new_ticker,
                    statement_date=new_date,
                )
            )

            similarity = company_name_similarity(
                old_company_name,
                new_company_name,
            )

            candidate_pairs.append({
                "old_index": old_index,
                "new_index": new_index,
                "old_ticker": old_ticker,
                "new_ticker": new_ticker,
                "detected_date": new_date,
                "carried_shares": (
                    missing_shares
                ),
                "old_company_name": (
                    old_company_name
                ),
                "new_company_name": (
                    new_company_name
                ),
                "company_name_similarity": (
                    similarity
                ),
                "day_gap": day_gap,
            })

    candidate_pairs.sort(
        key=lambda item: (
            -item["company_name_similarity"],
            item["day_gap"],
            item["old_ticker"],
            item["new_ticker"],
        )
    )

    used_old_indices: set[int] = set()
    used_new_indices: set[int] = set()
    aliases: list[dict[str, Any]] = []

    for candidate in candidate_pairs:
        old_index = candidate["old_index"]
        new_index = candidate["new_index"]

        if (
            old_index in used_old_indices
            or new_index in used_new_indices
        ):
            continue

        similarity = float(
            candidate[
                "company_name_similarity"
            ]
        )

        auto_apply = similarity >= 0.34

        confidence = (
            "VERIFIED"
            if auto_apply
            else "REVIEW_REQUIRED"
        )

        aliases.append({
            "old_ticker": (
                candidate["old_ticker"]
            ),
            "new_ticker": (
                candidate["new_ticker"]
            ),
            "detected_date": (
                candidate["detected_date"]
            ),
            "carried_shares": (
                candidate["carried_shares"]
            ),
            "old_company_name": (
                candidate["old_company_name"]
            ),
            "new_company_name": (
                candidate["new_company_name"]
            ),
            "company_name_similarity": (
                similarity
            ),
            "confidence": confidence,
            "auto_apply": auto_apply,
        })

        if not auto_apply:
            continue

        used_old_indices.add(old_index)
        used_new_indices.add(new_index)

        detail = (
            f"{candidate['old_ticker']} continued as "
            f"{candidate['new_ticker']} with the same "
            "unexplained share quantity and matching "
            "company identity."
        )

        checks.at[
            old_index,
            "resolution_status",
        ] = "RESOLVED"

        checks.at[
            old_index,
            "resolved_action",
        ] = "TICKER_CHANGE"

        checks.at[
            old_index,
            "resolved_ticker",
        ] = candidate["new_ticker"]

        checks.at[
            old_index,
            "confidence",
        ] = "VERIFIED"

        checks.at[
            old_index,
            "details",
        ] = detail

        checks.at[
            new_index,
            "resolution_status",
        ] = "RESOLVED"

        checks.at[
            new_index,
            "resolved_action",
        ] = "TICKER_CHANGE"

        checks.at[
            new_index,
            "resolved_ticker",
        ] = candidate["new_ticker"]

        checks.at[
            new_index,
            "confidence",
        ] = "VERIFIED"

        checks.at[
            new_index,
            "details",
        ] = detail

    return pd.DataFrame(
        aliases,
        columns=TICKER_ALIAS_COLUMNS,
    )


def build_verified_split_events(
    checks: pd.DataFrame,
    split_history: dict[
        str,
        list[tuple[date, float]],
    ],
) -> pd.DataFrame:
    """
    Return only split events that were used inside a
    successfully reconciled statement interval.

    Older unrelated splits for the same ticker must not be
    reported as verified by the uploaded statements.
    """

    rows: list[dict[str, Any]] = []
    seen_events: set[
        tuple[str, date, float]
    ] = set()

    verified_checks = checks.loc[
        (
            checks["resolution_status"]
            == "RESOLVED"
        )
        & (
            checks["resolved_action"]
            == "STOCK_SPLIT"
        )
    ]

    for _, check in verified_checks.iterrows():
        ticker = str(check["ticker"])

        previous_date = check[
            "previous_statement_date"
        ]

        statement_date = check[
            "statement_date"
        ]

        for split_date, ratio in (
            split_history.get(ticker, [])
        ):
            if split_date > statement_date:
                continue

            if (
                previous_date is not None
                and not pd.isna(previous_date)
                and split_date <= previous_date
            ):
                continue

            event_key = (
                ticker,
                split_date,
                float(ratio),
            )

            if event_key in seen_events:
                continue

            seen_events.add(event_key)

            rows.append({
                "ticker": ticker,
                "split_date": split_date,
                "split_ratio": float(ratio),
                "verified_by_holdings": True,
            })

    result = pd.DataFrame(
        rows,
        columns=SPLIT_EVENT_COLUMNS,
    )

    if result.empty:
        return result

    return result.sort_values(
        [
            "split_date",
            "ticker",
        ],
        kind="stable",
    ).reset_index(drop=True)

def resolve_corporate_actions(
    statements: CombinedMidasStatements,
    reconciliation: (
        MidasReconciliationResult | None
    ) = None,
    tolerance: float = 1e-8,
) -> CorporateActionResolutionResult:
    """
    Resolve timing differences, stock splits, and ticker
    changes without modifying transactions or the database.
    """

    if reconciliation is None:
        reconciliation = reconcile_midas_holdings(
            statements=statements,
            tolerance=tolerance,
        )

    issue_tickers = (
        reconciliation.issues["ticker"]
        .dropna()
        .astype(str)
        .str.upper()
        .unique()
        .tolist()
    )

    (
        split_history,
        split_lookup_errors,
    ) = download_split_history(issue_tickers)

    checks = build_split_adjusted_checks(
        statements=statements,
        reconciliation=reconciliation,
        split_history=split_history,
        tolerance=tolerance,
    )

    resolve_timing_differences(
        checks=checks,
        tolerance=tolerance,
    )

    ticker_aliases = resolve_ticker_changes(
        checks=checks,
        statements=statements,
        tolerance=tolerance,
    )

    split_events = build_verified_split_events(
        checks=checks,
        split_history=split_history,
    )

    unresolved_issues = (
        checks.loc[
            checks["resolution_status"]
            == "UNRESOLVED"
        ]
        .reset_index(drop=True)
    )

    resolved_count = int(
        (
            checks["resolution_status"]
            == "RESOLVED"
        ).sum()
    )

    action_counts = (
        checks.loc[
            checks["resolution_status"]
            == "RESOLVED",
            "resolved_action",
        ]
        .value_counts()
        .to_dict()
    )

    warnings = statements.warnings.copy()

    for ticker, message in (
        split_lookup_errors.items()
    ):
        warnings.append(
            f"Split lookup failed for {ticker}: "
            f"{message}"
        )

    if not unresolved_issues.empty:
        warnings.append(
            f"{len(unresolved_issues)} corporate-action "
            "difference remains unresolved."
        )

    summary = {
        "status": (
            "RESOLVED"
            if unresolved_issues.empty
            else "REVIEW_REQUIRED"
        ),
        "original_issue_count": len(
            reconciliation.issues
        ),
        "resolved_issue_count": resolved_count,
        "unresolved_issue_count": len(
            unresolved_issues
        ),
        "verified_split_count": len(
            split_events
        ),
        "ticker_alias_count": int(
            ticker_aliases["auto_apply"].sum()
        )
        if not ticker_aliases.empty
        else 0,
        "action_counts": action_counts,
        "split_lookup_error_count": len(
            split_lookup_errors
        ),
    }

    return CorporateActionResolutionResult(
        checks=checks,
        ticker_aliases=ticker_aliases,
        split_events=split_events,
        unresolved_issues=unresolved_issues,
        split_lookup_errors=(
            split_lookup_errors
        ),
        summary=summary,
        warnings=list(dict.fromkeys(warnings)),
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build terminal-test arguments.
    """

    parser = ArgumentParser(
        description=(
            "Resolve Midas statement timing, split, "
            "and ticker-change differences."
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
    Run resolution without writing Excel or database data.
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

    result = resolve_corporate_actions(
        statements=statements,
        reconciliation=reconciliation,
    )

    print()
    print("Corporate-action resolution completed.")
    print()

    for key, value in result.summary.items():
        print(f"{key}: {value}")

    if not result.split_events.empty:
        print()
        print("VERIFIED STOCK SPLITS")
        print()
        print(
            result.split_events.to_string(
                index=False
            )
        )

    if not result.ticker_aliases.empty:
        print()
        print("TICKER CHANGE CANDIDATES")
        print()
        print(
            result.ticker_aliases.to_string(
                index=False
            )
        )

    if not result.unresolved_issues.empty:
        print()
        print("UNRESOLVED ISSUES")
        print()
        print(
            result.unresolved_issues[
                [
                    "statement_date",
                    "ticker",
                    "previous_shares",
                    "observed_shares",
                    "split_adjusted_difference",
                    "details",
                ]
            ].to_string(index=False)
        )


if __name__ == "__main__":
    main()
