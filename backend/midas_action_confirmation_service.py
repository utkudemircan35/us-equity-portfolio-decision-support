from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Any

import pandas as pd

from midas_import_service import (
    CombinedMidasStatements,
)
from midas_official_action_resolver import (
    OfficialActionResolutionResult,
)
from midas_pdf_parser import TRADE_COLUMNS


ALLOWED_DECISIONS = {
    "APPROVE",
    "REJECT",
}

SYNTHETIC_SOURCE_FILE = (
    "SEC_CORPORATE_ACTION"
)


@dataclass
class ActionConfirmationResult:
    """
    User-reviewed corporate actions and merged trades.

    The input statements are never modified in place.
    """

    review_actions: pd.DataFrame
    approved_actions: pd.DataFrame
    rejected_actions: pd.DataFrame
    combined_trades: pd.DataFrame
    summary: dict[str, Any]


def normalize_action_date(
    value: Any,
) -> date:
    """
    Convert an action date to a Python date.
    """

    converted = pd.to_datetime(
        value,
        errors="coerce",
    )

    if pd.isna(converted):
        raise ValueError(
            f"Invalid corporate-action date: {value}"
        )

    return converted.date()


def build_action_id(
    row: pd.Series,
) -> str:
    """
    Build a stable ID for one proposed synthetic trade.
    """

    action_date = normalize_action_date(
        row["date"]
    ).isoformat()

    payload = "|".join([
        str(row["ticker"]).strip().upper(),
        str(row["action_type"]).strip().upper(),
        action_date,
        f"{float(row['shares']):.10f}",
        f"{float(row['price_usd']):.10f}",
    ])

    digest = sha256(
        payload.encode("utf-8")
    ).hexdigest()

    return f"midas-action-{digest[:20]}"


def prepare_action_review(
    resolution: OfficialActionResolutionResult,
) -> pd.DataFrame:
    """
    Add stable IDs and pending decisions to the preview.
    """

    preview = (
        resolution
        .synthetic_trades_preview
        .copy()
        .reset_index(drop=True)
    )

    required_columns = {
        "date",
        "ticker",
        "side",
        "shares",
        "price_usd",
        "commission_usd",
        "is_gift",
        "action_type",
    }

    missing_columns = sorted(
        required_columns - set(preview.columns)
    )

    if missing_columns:
        raise ValueError(
            "Synthetic trade preview is missing "
            "required columns: "
            + ", ".join(missing_columns)
        )

    preview.insert(
        0,
        "action_id",
        [
            build_action_id(row)
            for _, row in preview.iterrows()
        ],
    )

    if preview["action_id"].duplicated().any():
        raise ValueError(
            "Duplicate corporate-action candidates "
            "were generated."
        )

    preview["decision"] = "PENDING"

    return preview


def normalize_decisions(
    decisions: Mapping[str, str],
    known_action_ids: set[str],
) -> dict[str, str]:
    """
    Validate frontend or terminal decisions.
    """

    normalized: dict[str, str] = {}

    for action_id, decision in decisions.items():
        normalized_id = str(action_id).strip()

        normalized_decision = (
            str(decision).strip().upper()
        )

        if normalized_id not in known_action_ids:
            raise ValueError(
                "Unknown corporate-action ID: "
                f"{normalized_id}"
            )

        if normalized_decision not in ALLOWED_DECISIONS:
            raise ValueError(
                "Decision must be APPROVE or REJECT "
                f"for {normalized_id}."
            )

        normalized[normalized_id] = (
            normalized_decision
        )

    return normalized


def build_confirmed_trade_rows(
    approved_actions: pd.DataFrame,
) -> pd.DataFrame:
    """
    Convert approved previews to raw Midas trade rows.
    """

    rows: list[dict[str, Any]] = []

    for _, action in approved_actions.iterrows():
        trade_date = normalize_action_date(
            action["date"]
        )

        shares = float(action["shares"])

        price_usd = float(
            action["price_usd"]
        )

        rows.append({
            "date": trade_date,
            "time": action.get("time"),
            "ticker": (
                str(action["ticker"])
                .strip()
                .upper()
            ),
            "side": "Sell",
            "shares": shares,
            "price_usd": price_usd,
            "commission_usd": float(
                action["commission_usd"]
            ),
            "gross_amount_usd": (
                shares * price_usd
            ),
            "is_gift": False,
            "source_file": (
                SYNTHETIC_SOURCE_FILE
            ),
            "source_hash": action[
                "action_id"
            ],
            "statement_start": trade_date,
            "statement_end": trade_date,
            "source_page": None,
        })

    return pd.DataFrame(
        rows,
        columns=TRADE_COLUMNS,
    )


def merge_confirmed_trades(
    original_trades: pd.DataFrame,
    confirmed_trades: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """
    Merge confirmed trades without creating duplicates.
    """

    existing = original_trades[
        TRADE_COLUMNS
    ].copy()

    if confirmed_trades.empty:
        return existing, 0

    existing_action_ids = set(
        existing.loc[
            existing["source_file"]
            == SYNTHETIC_SOURCE_FILE,
            "source_hash",
        ]
        .dropna()
        .astype(str)
        .tolist()
    )

    new_rows = confirmed_trades.loc[
        ~confirmed_trades[
            "source_hash"
        ]
        .astype(str)
        .isin(existing_action_ids)
    ].copy()

    combined = pd.concat(
        [existing, new_rows],
        ignore_index=True,
    )

    combined["_source_order"] = range(
        len(combined)
    )

    combined["_sort_date"] = pd.to_datetime(
        combined["date"],
        errors="raise",
    )

    combined["_sort_time"] = (
        combined["time"]
        .apply(
            lambda value: (
                "00:00:00"
                if pd.isna(value)
                else (
                    value.strftime("%H:%M:%S")
                    if hasattr(value, "strftime")
                    else str(value)
                )
            )
        )
    )

    combined = (
        combined.sort_values(
            by=[
                "_sort_date",
                "_sort_time",
                "_source_order",
            ],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    return (
        combined[TRADE_COLUMNS].copy(),
        len(new_rows),
    )


def apply_action_decisions(
    statements: CombinedMidasStatements,
    resolution: OfficialActionResolutionResult,
    decisions: Mapping[str, str],
    require_all_decisions: bool = True,
) -> ActionConfirmationResult:
    """
    Apply explicit decisions to proposed synthetic trades.

    APPROVE adds the proposed trade. REJECT never adds it.
    """

    review_actions = prepare_action_review(
        resolution
    )

    known_action_ids = set(
        review_actions["action_id"]
        .astype(str)
        .tolist()
    )

    normalized_decisions = normalize_decisions(
        decisions=decisions,
        known_action_ids=known_action_ids,
    )

    mapped_decisions = (
        review_actions["action_id"]
        .map(normalized_decisions)
        .astype("string")
    )

    review_actions["decision"] = (
        mapped_decisions
        .fillna("PENDING")
        .astype(str)
    )

    pending_actions = review_actions.loc[
        review_actions["decision"]
        == "PENDING"
    ]

    if (
        require_all_decisions
        and not pending_actions.empty
    ):
        pending_ids = ", ".join(
            pending_actions["action_id"]
            .astype(str)
            .tolist()
        )

        raise ValueError(
            "Every corporate action must be "
            "approved or rejected. Pending IDs: "
            + pending_ids
        )

    approved_actions = review_actions.loc[
        review_actions["decision"]
        == "APPROVE"
    ].copy()

    rejected_actions = review_actions.loc[
        review_actions["decision"]
        == "REJECT"
    ].copy()

    confirmed_trades = (
        build_confirmed_trade_rows(
            approved_actions
        )
    )

    combined_trades, applied_count = (
        merge_confirmed_trades(
            original_trades=statements.trades,
            confirmed_trades=confirmed_trades,
        )
    )

    unresolved_count = len(
        resolution.unresolved_issues
    )

    if unresolved_count:
        status = "REVIEW_REQUIRED"

    elif not pending_actions.empty:
        status = "AWAITING_USER_CONFIRMATION"

    else:
        status = "READY_FOR_EXPORT"

    summary = {
        "status": status,
        "candidate_count": len(
            review_actions
        ),
        "approved_count": len(
            approved_actions
        ),
        "rejected_count": len(
            rejected_actions
        ),
        "pending_count": len(
            pending_actions
        ),
        "unresolved_issue_count": (
            unresolved_count
        ),
        "applied_trade_count": (
            applied_count
        ),
        "combined_trade_count": len(
            combined_trades
        ),
    }

    return ActionConfirmationResult(
        review_actions=review_actions,
        approved_actions=approved_actions,
        rejected_actions=rejected_actions,
        combined_trades=combined_trades,
        summary=summary,
    )
