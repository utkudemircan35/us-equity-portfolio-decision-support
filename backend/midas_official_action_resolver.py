from __future__ import annotations

from argparse import ArgumentParser
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
import re

import pandas as pd

from midas_cash_evidence_service import (
    CashAcquisitionEvidenceResult,
    find_cash_evidence,
)
from midas_corporate_action_resolver import (
    CorporateActionResolutionResult,
    latest_company_name,
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
from sec_corporate_action_service import (
    SecCashAcquisitionResult,
    build_sec_session,
    find_sec_cash_acquisition,
    html_to_text,
)


MONTH_PATTERN = (
    r"(?:January|February|March|April|May|June|"
    r"July|August|September|October|November|"
    r"December)"
)

WRITTEN_DATE_PATTERN = (
    rf"{MONTH_PATTERN}\s+\d{{1,2}},\s+\d{{4}}"
)

OFFICIAL_ACTION_COLUMNS = [
    "ticker",
    "company_name",
    "action_type",
    "effective_date",
    "market_exit_date",
    "synthetic_trade_date",
    "cash_price_per_share",
    "shares",
    "expected_proceeds_usd",
    "rounded_proceeds_usd",
    "cash_evidence_amount_usd",
    "cash_evidence_matches",
    "supporting_document_count",
    "source_url",
    "confidence",
]

SYNTHETIC_TRADE_COLUMNS = [
    "date",
    "time",
    "ticker",
    "side",
    "shares",
    "price_usd",
    "commission_usd",
    "is_gift",
    "action_type",
    "confidence",
    "expected_proceeds_usd",
    "cash_evidence_amount_usd",
    "cash_evidence_matches",
    "source_url",
]


@dataclass
class OfficialActionResolutionResult:
    """
    Officially verified actions and proposed synthetic
    transactions.

    Nothing in this result is written to Excel or SQLite.
    """

    official_actions: pd.DataFrame
    synthetic_trades_preview: pd.DataFrame
    unresolved_issues: pd.DataFrame
    summary: dict[str, Any]
    warnings: list[str]


def parse_written_date(
    value: str,
) -> date | None:
    """
    Parse dates such as October 27, 2022.
    """

    try:
        return datetime.strptime(
            value,
            "%B %d, %Y",
        ).date()

    except ValueError:
        return None


def dates_inside_window(
    values: list[date],
    start_date: date,
    end_date: date,
) -> list[date]:
    """
    Keep plausible event dates inside the search window.
    """

    return [
        value
        for value in values
        if start_date <= value <= end_date
    ]


def extract_effective_dates(
    text: str,
    start_date: date,
    end_date: date,
) -> list[date]:
    """
    Extract merger completion/effective dates.
    """

    patterns = [
        re.compile(
            rf"\bOn\s+"
            rf"(?P<date>{WRITTEN_DATE_PATTERN})"
            rf".{{0,650}}?"
            rf"(?:merger|acquisition)"
            rf".{{0,300}}?"
            rf"(?:consummated|completed|"
            rf"became effective|was effective)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            rf"\bOn\s+"
            rf"(?P<date>{WRITTEN_DATE_PATTERN})"
            rf".{{0,650}}?"
            rf"(?:merged with|merged into)",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            rf"(?:merger|acquisition)"
            rf".{{0,300}}?"
            rf"(?:consummated|completed|"
            rf"became effective|was effective)"
            rf".{{0,180}}?"
            rf"(?P<date>{WRITTEN_DATE_PATTERN})",
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    values: list[date] = []

    for pattern in patterns:
        for match in pattern.finditer(text):
            parsed = parse_written_date(
                match.group("date")
            )

            if parsed is not None:
                values.append(parsed)

    return dates_inside_window(
        values,
        start_date,
        end_date,
    )


def extract_market_exit_dates(
    text: str,
    start_date: date,
    end_date: date,
) -> list[date]:
    """
    Extract delisting or trading-suspension dates.
    """

    patterns = [
        re.compile(
            rf"(?:suspended from trading|"
            rf"trading.{{0,80}}?suspended|"
            rf"delist(?:ed|ing)?)"
            rf".{{0,220}}?"
            rf"(?P<date>{WRITTEN_DATE_PATTERN})",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            rf"\bOn\s+"
            rf"(?P<date>{WRITTEN_DATE_PATTERN})"
            rf".{{0,500}}?"
            rf"(?:suspended from trading|"
            rf"trading.{{0,80}}?suspended|"
            rf"delist(?:ed|ing)?)",
            re.IGNORECASE | re.DOTALL,
        ),
    ]

    values: list[date] = []

    for pattern in patterns:
        for match in pattern.finditer(text):
            parsed = parse_written_date(
                match.group("date")
            )

            if parsed is not None:
                values.append(parsed)

    return dates_inside_window(
        values,
        start_date,
        end_date,
    )


def most_common_date(
    values: list[date],
) -> date | None:
    """
    Select the date supported most often.
    """

    if not values:
        return None

    return Counter(values).most_common(1)[0][0]


def resolve_official_dates(
    sec_result: SecCashAcquisitionResult,
    start_date: date,
    end_date: date,
    sec_user_agent: str | None = None,
) -> tuple[
    date | None,
    date | None,
]:
    """
    Keep legal effective date and market exit date separate.

    The complete best SEC filing is inspected because the
    short price context may not contain the event dates.
    """

    text_parts = [
        evidence.context
        for evidence in sec_result.evidence
        if evidence.context
    ]

    document_urls: list[str] = []

    if sec_result.best_document_url:
        document_urls.append(
            sec_result.best_document_url
        )

    for evidence in sec_result.evidence:
        document_url = getattr(
            evidence.hit,
            "document_url",
            None,
        )

        if (
            document_url
            and document_url not in document_urls
        ):
            document_urls.append(document_url)

    if document_urls:
        try:
            session = build_sec_session(
                sec_user_agent=sec_user_agent
            )

            for document_url in document_urls:
                try:
                    response = session.get(
                        document_url,
                        timeout=30,
                    )

                    response.raise_for_status()

                    complete_filing_text = (
                        html_to_text(
                            response.text
                        )
                    )

                    if complete_filing_text:
                        text_parts.append(
                            complete_filing_text
                        )

                except Exception:
                    continue

        except Exception:
            # The original SEC lookup result remains usable.
            # Missing full-document access is handled by the
            # fallback date below.
            pass

    evidence_text = " ".join(text_parts)

    effective_date = most_common_date(
        extract_effective_dates(
            text=evidence_text,
            start_date=start_date,
            end_date=end_date,
        )
    )

    market_exit_date = most_common_date(
        extract_market_exit_dates(
            text=evidence_text,
            start_date=start_date,
            end_date=end_date,
        )
    )

    fallback_date = sec_result.event_date

    if (
        market_exit_date is None
        and fallback_date is not None
    ):
        market_exit_date = fallback_date

    filing_date = (
        sec_result.best_document_filing_date
    )

    if (
        market_exit_date is None
        and filing_date is not None
        and start_date <= filing_date <= end_date
        and sec_result.best_document_form
        in {
            "8-K",
            "SC 13D",
            "SC 13D/A",
            "25-NSE",
        }
    ):
        market_exit_date = filing_date

    return (
        effective_date,
        market_exit_date,
    )

def round_usd(
    value: float,
) -> float:
    """
    Round USD using financial half-up rounding.
    """

    return float(
        Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def find_best_cash_candidate(
    cash_evidence: CashAcquisitionEvidenceResult,
    ticker: str,
) -> pd.Series | None:
    """
    Return the highest-ranked Midas cash evidence.
    """

    if cash_evidence.best_candidates.empty:
        return None

    rows = cash_evidence.best_candidates.loc[
        cash_evidence.best_candidates[
            "ticker"
        ]
        .astype(str)
        .str.upper()
        == ticker
    ]

    if rows.empty:
        return None

    return rows.iloc[0]


def cash_amount_matches(
    expected_rounded_proceeds: float,
    cash_amount: float,
    tolerance: float = 0.01,
) -> bool:
    """
    Compare rounded corporate proceeds with a Midas cash
    movement.
    """

    return (
        abs(
            expected_rounded_proceeds
            - cash_amount
        )
        <= tolerance
    )


def resolve_official_cash_actions(
    statements: CombinedMidasStatements,
    corporate_actions: CorporateActionResolutionResult,
    cash_evidence: CashAcquisitionEvidenceResult,
    sec_user_agent: str | None = None,
    search_days_before: int = 120,
    search_days_after: int = 30,
    tolerance: float = 1e-8,
) -> OfficialActionResolutionResult:
    """
    Resolve remaining position disappearances with official
    SEC filings.

    The returned Sell rows are preview-only.
    """

    action_rows: list[dict[str, Any]] = []
    synthetic_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for _, issue in (
        corporate_actions.unresolved_issues
        .iterrows()
    ):
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
            unresolved_rows.append(
                issue.to_dict()
            )
            continue

        statement_date = issue[
            "statement_date"
        ]

        previous_statement_date = issue[
            "previous_statement_date"
        ]

        company_name = latest_company_name(
            holdings=statements.holdings,
            ticker=ticker,
            statement_date=(
                previous_statement_date
                or statement_date
            ),
        )

        if not company_name:
            company_name = ticker

        search_start = (
            (
                previous_statement_date
                or statement_date
            )
            - timedelta(
                days=search_days_before
            )
        )

        search_end = (
            statement_date
            + timedelta(
                days=search_days_after
            )
        )

        best_cash_candidate = (
            find_best_cash_candidate(
                cash_evidence=cash_evidence,
                ticker=ticker,
            )
        )

        expected_price_per_share = None

        if best_cash_candidate is not None:
            implied_price = (
                best_cash_candidate.get(
                    "implied_price_per_share"
                )
            )

            if (
                pd.notna(implied_price)
                and float(implied_price) > 0
            ):
                expected_price_per_share = float(
                    implied_price
                )

        sec_result = find_sec_cash_acquisition(
            ticker=ticker,
            company_name=company_name,
            start_date=search_start,
            end_date=search_end,
            sec_user_agent=sec_user_agent,
            expected_price_per_share=(
                expected_price_per_share
            ),
        )

        if (
            sec_result.status != "FOUND"
            or sec_result.cash_price_per_share
            is None
        ):
            unresolved_row = issue.to_dict()
            unresolved_row[
                "official_lookup_status"
            ] = sec_result.status

            unresolved_rows.append(
                unresolved_row
            )

            warnings.extend(
                sec_result.warnings
            )
            continue

        (
            effective_date,
            market_exit_date,
        ) = resolve_official_dates(
            sec_result=sec_result,
            start_date=search_start,
            end_date=search_end,
            sec_user_agent=sec_user_agent,
        )

        synthetic_trade_date = (
            market_exit_date
            or effective_date
            or sec_result.event_date
        )

        if synthetic_trade_date is None:
            unresolved_row = issue.to_dict()
            unresolved_row[
                "official_lookup_status"
            ] = "DATE_NOT_FOUND"

            unresolved_rows.append(
                unresolved_row
            )

            warnings.append(
                f"Official cash price was found for "
                f"{ticker}, but no event date was found."
            )
            continue

        missing_shares = abs(difference)

        cash_price = float(
            sec_result.cash_price_per_share
        )

        expected_proceeds = (
            missing_shares * cash_price
        )

        rounded_proceeds = round_usd(
            expected_proceeds
        )

        cash_evidence_amount = None
        cash_evidence_matches_value = False

        if best_cash_candidate is not None:
            cash_evidence_amount = float(
                best_cash_candidate[
                    "cash_amount_usd"
                ]
            )

            cash_evidence_matches_value = (
                cash_amount_matches(
                    expected_rounded_proceeds=(
                        rounded_proceeds
                    ),
                    cash_amount=(
                        cash_evidence_amount
                    ),
                )
            )

        if (
            best_cash_candidate is not None
            and not cash_evidence_matches_value
        ):
            unresolved_row = issue.to_dict()

            unresolved_row[
                "official_lookup_status"
            ] = "CASH_EVIDENCE_MISMATCH"

            unresolved_row[
                "official_cash_price_per_share"
            ] = cash_price

            unresolved_row[
                "official_expected_proceeds_usd"
            ] = rounded_proceeds

            unresolved_row[
                "cash_evidence_amount_usd"
            ] = cash_evidence_amount

            unresolved_rows.append(
                unresolved_row
            )

            warnings.append(
                f"Official SEC proceeds for "
                f"{ticker} were $"
                f"{rounded_proceeds:.2f}, but "
                "Midas cash evidence was $"
                f"{cash_evidence_amount:.2f}. "
                "The proposed trade was blocked."
            )

            warnings.extend(
                sec_result.warnings
            )

            continue

        confidence = (
            "VERIFIED_WITH_CASH_EVIDENCE"
            if cash_evidence_matches_value
            else "VERIFIED_BY_SEC"
        )

        action_rows.append({
            "ticker": ticker,
            "company_name": company_name,
            "action_type": (
                "CASH_ACQUISITION"
            ),
            "effective_date": effective_date,
            "market_exit_date": (
                market_exit_date
            ),
            "synthetic_trade_date": (
                synthetic_trade_date
            ),
            "cash_price_per_share": (
                cash_price
            ),
            "shares": missing_shares,
            "expected_proceeds_usd": (
                expected_proceeds
            ),
            "rounded_proceeds_usd": (
                rounded_proceeds
            ),
            "cash_evidence_amount_usd": (
                cash_evidence_amount
            ),
            "cash_evidence_matches": (
                cash_evidence_matches_value
            ),
            "supporting_document_count": (
                sec_result
                .supporting_document_count
            ),
            "source_url": (
                sec_result.best_document_url
            ),
            "confidence": confidence,
        })

        synthetic_rows.append({
            "date": synthetic_trade_date,
            "time": None,
            "ticker": ticker,
            "side": "Sell",
            "shares": missing_shares,
            "price_usd": cash_price,
            "commission_usd": 0.0,
            "is_gift": False,
            "action_type": (
                "CASH_ACQUISITION"
            ),
            "confidence": confidence,
            "expected_proceeds_usd": (
                expected_proceeds
            ),
            "cash_evidence_amount_usd": (
                cash_evidence_amount
            ),
            "cash_evidence_matches": (
                cash_evidence_matches_value
            ),
            "source_url": (
                sec_result.best_document_url
            ),
        })

        warnings.extend(
            sec_result.warnings
        )

    official_actions = pd.DataFrame(
        action_rows,
        columns=OFFICIAL_ACTION_COLUMNS,
    )

    synthetic_trades_preview = pd.DataFrame(
        synthetic_rows,
        columns=SYNTHETIC_TRADE_COLUMNS,
    )

    unresolved_issues = pd.DataFrame(
        unresolved_rows
    )

    summary = {
        "status": (
            "READY_FOR_USER_CONFIRMATION"
            if unresolved_issues.empty
            else "REVIEW_REQUIRED"
        ),
        "official_action_count": len(
            official_actions
        ),
        "synthetic_trade_count": len(
            synthetic_trades_preview
        ),
        "unresolved_issue_count": len(
            unresolved_issues
        ),
        "cash_evidence_match_count": int(
            official_actions[
                "cash_evidence_matches"
            ].sum()
        )
        if not official_actions.empty
        else 0,
    }

    return OfficialActionResolutionResult(
        official_actions=official_actions,
        synthetic_trades_preview=(
            synthetic_trades_preview
        ),
        unresolved_issues=unresolved_issues,
        summary=summary,
        warnings=list(dict.fromkeys(warnings)),
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build terminal-test arguments.
    """

    parser = ArgumentParser(
        description=(
            "Prepare preview-only synthetic trades from "
            "official SEC corporate-action evidence."
        )
    )

    parser.add_argument(
        "--folder",
        type=Path,
        required=True,
    )

    return parser


def main() -> None:
    """
    Build the official action preview.
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

    cash_evidence = find_cash_evidence(
        statements=statements,
        corporate_actions=corporate_actions,
    )

    result = resolve_official_cash_actions(
        statements=statements,
        corporate_actions=corporate_actions,
        cash_evidence=cash_evidence,
    )

    print()
    print("Official corporate-action preview completed.")
    print()

    for key, value in result.summary.items():
        print(f"{key}: {value}")

    if not result.official_actions.empty:
        print()
        print("OFFICIAL ACTIONS")
        print()
        print(
            result.official_actions.to_string(
                index=False
            )
        )

    if not result.synthetic_trades_preview.empty:
        print()
        print("SYNTHETIC TRADE PREVIEW")
        print()

        print(
            result.synthetic_trades_preview.to_string(
                index=False
            )
        )

    if not result.unresolved_issues.empty:
        print()
        print("UNRESOLVED OFFICIAL ACTIONS")
        print()

        display_columns = [
            column
            for column in [
                "statement_date",
                "ticker",
                "previous_shares",
                "observed_shares",
                "split_adjusted_difference",
                "official_lookup_status",
            ]
            if column
            in result.unresolved_issues.columns
        ]

        print(
            result.unresolved_issues[
                display_columns
            ].to_string(index=False)
        )

    if result.warnings:
        print()
        print("WARNINGS")
        print()

        for warning in result.warnings:
            print(f"- {warning}")

if __name__ == "__main__":
    main()
