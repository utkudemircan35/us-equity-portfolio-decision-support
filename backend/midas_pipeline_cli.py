from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable
import os
from pathlib import Path
from typing import Any

import pandas as pd

from midas_import_service import (
    collect_pdf_paths,
)
from midas_portfolio_exporter import (
    save_midas_portfolio_workbook,
)
from midas_portfolio_pipeline import (
    MidasPortfolioPipelineResult,
    console_pipeline_progress,
    finalize_midas_portfolio_import,
    prepare_midas_portfolio_import,
)


APP_NAME = (
    "US Equity Portfolio Decision Support"
)


def build_sec_user_agent(
    contact_email: str | None,
) -> str | None:
    """
    Build the SEC identity for a local application operator.
    """

    if contact_email is None:
        return None

    normalized_email = contact_email.strip()

    if (
        not normalized_email
        or "@" not in normalized_email
    ):
        raise ValueError(
            "SEC contact email is invalid."
        )

    return f"{APP_NAME} {normalized_email}"


def resolve_cli_sec_user_agent(
    contact_email: str | None,
    input_function: Callable[
        [str],
        str,
    ] = input,
) -> str:
    """
    Resolve SEC identification before reading any PDFs.

    A command-line email takes priority. Otherwise an
    existing SEC_USER_AGENT value is used. If neither is
    available, ask the local user immediately so the long
    import cannot fail later at the SEC step.
    """

    explicit_user_agent = build_sec_user_agent(
        contact_email
    )

    if explicit_user_agent is not None:
        return explicit_user_agent

    environment_user_agent = (
        os.getenv("SEC_USER_AGENT", "").strip()
    )

    if environment_user_agent:
        if "@" not in environment_user_agent:
            raise ValueError(
                "SEC_USER_AGENT must include a contact "
                "email address."
            )

        return environment_user_agent

    entered_email = input_function(
        "SEC contact email: "
    )

    prompted_user_agent = build_sec_user_agent(
        entered_email
    )

    if prompted_user_agent is None:
        raise ValueError(
            "SEC contact email is required."
        )

    return prompted_user_agent


def print_summary(
    title: str,
    summary: dict[str, Any],
) -> None:
    """
    Print a compact result summary.
    """

    print()
    print(title)
    print()

    for key, value in summary.items():
        print(f"{key}: {value}")


def display_action_review(
    review: pd.DataFrame,
) -> None:
    """
    Display only user-relevant review fields.
    """

    if review.empty:
        return

    display_columns = [
        column
        for column in [
            "action_id",
            "date",
            "ticker",
            "shares",
            "price_usd",
            "expected_proceeds_usd",
            "cash_evidence_amount_usd",
            "cash_evidence_matches",
            "confidence",
        ]
        if column in review.columns
    ]

    print()
    print("CORPORATE ACTION REVIEW")
    print()

    print(
        review[display_columns].to_string(
            index=False
        )
    )


def ask_action_decisions(
    review: pd.DataFrame,
    approve_all: bool = False,
    reject_all: bool = False,
    input_function: Callable[
        [str],
        str,
    ] = input,
) -> dict[str, str]:
    """
    Collect explicit decisions for every proposed action.
    """

    if approve_all and reject_all:
        raise ValueError(
            "approve_all and reject_all cannot "
            "both be enabled."
        )

    if review.empty:
        return {}

    action_ids = (
        review["action_id"]
        .astype(str)
        .tolist()
    )

    if approve_all:
        return {
            action_id: "APPROVE"
            for action_id in action_ids
        }

    if reject_all:
        return {
            action_id: "REJECT"
            for action_id in action_ids
        }

    decisions: dict[str, str] = {}

    approve_answers = {
        "y",
        "yes",
        "e",
        "evet",
    }

    reject_answers = {
        "n",
        "no",
        "h",
        "hayir",
        "hayır",
    }

    for _, action in review.iterrows():
        action_id = str(
            action["action_id"]
        )

        ticker = str(action["ticker"])
        action_date = action["date"]
        shares = float(action["shares"])
        price = float(action["price_usd"])

        while True:
            answer = input_function(
                f"Approve {ticker} Sell on "
                f"{action_date} for {shares:.6f} "
                "shares at $"
                f"{price:.4f}? [y/n]: "
            )

            normalized_answer = (
                str(answer)
                .strip()
                .casefold()
            )

            if normalized_answer in approve_answers:
                decisions[action_id] = (
                    "APPROVE"
                )
                break

            if normalized_answer in reject_answers:
                decisions[action_id] = (
                    "REJECT"
                )
                break

            print(
                "Please answer y/yes or n/no."
            )

    return decisions


def run_midas_pipeline(
    folder: str | Path,
    output: str | Path,
    sec_contact_email: str | None = None,
    approve_all: bool = False,
    reject_all: bool = False,
    overwrite: bool = False,
    input_function: Callable[
        [str],
        str,
    ] = input,
) -> MidasPortfolioPipelineResult:
    """
    Run the complete local Midas conversion workflow.
    """

    sec_user_agent = resolve_cli_sec_user_agent(
        contact_email=sec_contact_email,
        input_function=input_function,
    )

    pdf_paths = collect_pdf_paths(
        Path(folder)
    )

    preview = prepare_midas_portfolio_import(
        sources=pdf_paths,
        sec_user_agent=sec_user_agent,
        progress_callback=(
            console_pipeline_progress
        ),
    )

    print_summary(
        title="MIDAS IMPORT PREVIEW",
        summary=preview.summary,
    )

    if preview.warnings:
        print()
        print("WARNINGS")
        print()

        for warning in preview.warnings:
            print(f"- {warning}")

    display_action_review(
        preview.action_review
    )

    if (
        preview.summary.get("status")
        == "REVIEW_REQUIRED"
    ):
        if not (
            preview
            .official_actions
            .unresolved_issues
            .empty
        ):
            print()
            print("UNRESOLVED ISSUES")
            print()

            print(
                preview
                .official_actions
                .unresolved_issues
                .to_string(index=False)
            )

        raise ValueError(
            "Unresolved corporate actions must "
            "be reviewed before export."
        )

    decisions = ask_action_decisions(
        review=preview.action_review,
        approve_all=approve_all,
        reject_all=reject_all,
        input_function=input_function,
    )

    result = finalize_midas_portfolio_import(
        preview=preview,
        decisions=decisions,
        progress_callback=(
            console_pipeline_progress
        ),
    )

    saved_path = save_midas_portfolio_workbook(
        workbook=result.workbook,
        destination=output,
        overwrite=overwrite,
    )

    print_summary(
        title="MIDAS IMPORT COMPLETED",
        summary=result.summary,
    )

    print()
    print(
        f"Output file: "
        f"{saved_path.resolve()}"
    )

    return result


def build_argument_parser() -> ArgumentParser:
    """
    Build command-line arguments.
    """

    parser = ArgumentParser(
        description=(
            "Convert Midas PDF statements into a "
            "portfolio.xlsx-compatible workbook."
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

    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=(
            "Destination .xlsx file."
        ),
    )

    parser.add_argument(
        "--sec-contact-email",
        help=(
            "Local application operator email for "
            "SEC fair-access identification. "
            "Optional when SEC_USER_AGENT is set."
        ),
    )

    decision_group = (
        parser.add_mutually_exclusive_group()
    )

    decision_group.add_argument(
        "--approve-all",
        action="store_true",
        help=(
            "Explicitly approve every proposed "
            "corporate action."
        ),
    )

    decision_group.add_argument(
        "--reject-all",
        action="store_true",
        help=(
            "Explicitly reject every proposed "
            "corporate action."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Allow replacement of an existing "
            "output file."
        ),
    )

    return parser


def main() -> None:
    """
    Run the command-line workflow.
    """

    arguments = (
        build_argument_parser().parse_args()
    )

    run_midas_pipeline(
        folder=arguments.folder,
        output=arguments.output,
        sec_contact_email=(
            arguments.sec_contact_email
        ),
        approve_all=arguments.approve_all,
        reject_all=arguments.reject_all,
        overwrite=arguments.overwrite,
    )


if __name__ == "__main__":
    main()
