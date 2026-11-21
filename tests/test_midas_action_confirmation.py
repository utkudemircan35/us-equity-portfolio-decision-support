from datetime import date

import pandas as pd
import pytest

from midas_action_confirmation_service import (
    apply_action_decisions,
    prepare_action_review,
)
from midas_import_service import (
    CombinedMidasStatements,
)
from midas_official_action_resolver import (
    OFFICIAL_ACTION_COLUMNS,
    SYNTHETIC_TRADE_COLUMNS,
    OfficialActionResolutionResult,
)
from midas_pdf_parser import (
    CASH_MOVEMENT_COLUMNS,
    DIVIDEND_COLUMNS,
    HOLDING_COLUMNS,
    TRADE_COLUMNS,
    UNSUPPORTED_ACTIVITY_COLUMNS,
)


def empty_frame(
    columns: list[str],
) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def build_statements(
    trades: pd.DataFrame | None = None,
) -> CombinedMidasStatements:
    return CombinedMidasStatements(
        trades=(
            trades.copy()
            if trades is not None
            else empty_frame(TRADE_COLUMNS)
        ),
        cash_movements=empty_frame(
            CASH_MOVEMENT_COLUMNS
        ),
        dividends=empty_frame(
            DIVIDEND_COLUMNS
        ),
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


def build_resolution(
    unresolved: bool = False,
) -> OfficialActionResolutionResult:
    preview = pd.DataFrame(
        [
            {
                "date": date(2022, 10, 28),
                "time": None,
                "ticker": "TWTR",
                "side": "Sell",
                "shares": 0.221877,
                "price_usd": 54.20,
                "commission_usd": 0.0,
                "is_gift": False,
                "action_type": (
                    "CASH_ACQUISITION"
                ),
                "confidence": (
                    "VERIFIED_WITH_CASH_EVIDENCE"
                ),
                "expected_proceeds_usd": (
                    12.0257334
                ),
                "cash_evidence_amount_usd": (
                    12.03
                ),
                "cash_evidence_matches": True,
                "source_url": (
                    "https://www.sec.gov/example"
                ),
            }
        ],
        columns=SYNTHETIC_TRADE_COLUMNS,
    )

    unresolved_issues = (
        pd.DataFrame([{"ticker": "UNKNOWN"}])
        if unresolved
        else pd.DataFrame()
    )

    return OfficialActionResolutionResult(
        official_actions=empty_frame(
            OFFICIAL_ACTION_COLUMNS
        ),
        synthetic_trades_preview=preview,
        unresolved_issues=unresolved_issues,
        summary={},
        warnings=[],
    )


def get_action_id(
    resolution: OfficialActionResolutionResult,
) -> str:
    review = prepare_action_review(
        resolution
    )

    return str(
        review.iloc[0]["action_id"]
    )


def test_action_id_is_stable() -> None:
    resolution = build_resolution()

    first_id = get_action_id(resolution)
    second_id = get_action_id(resolution)

    assert first_id == second_id

    assert first_id.startswith(
        "midas-action-"
    )


def test_approved_action_is_added() -> None:
    statements = build_statements()
    resolution = build_resolution()
    action_id = get_action_id(resolution)

    result = apply_action_decisions(
        statements=statements,
        resolution=resolution,
        decisions={
            action_id: "APPROVE",
        },
    )

    assert result.summary["status"] == (
        "READY_FOR_EXPORT"
    )

    assert result.summary[
        "applied_trade_count"
    ] == 1

    assert len(result.combined_trades) == 1

    trade = result.combined_trades.iloc[0]

    assert trade["ticker"] == "TWTR"
    assert trade["side"] == "Sell"

    assert trade["shares"] == pytest.approx(
        0.221877
    )

    assert trade["price_usd"] == pytest.approx(
        54.20
    )

    assert trade["source_hash"] == action_id


def test_rejected_action_is_not_added() -> None:
    statements = build_statements()
    resolution = build_resolution()
    action_id = get_action_id(resolution)

    result = apply_action_decisions(
        statements=statements,
        resolution=resolution,
        decisions={
            action_id: "REJECT",
        },
    )

    assert result.summary[
        "rejected_count"
    ] == 1

    assert result.summary[
        "applied_trade_count"
    ] == 0

    assert result.combined_trades.empty


def test_pending_decision_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="Every corporate action",
    ):
        apply_action_decisions(
            statements=build_statements(),
            resolution=build_resolution(),
            decisions={},
        )


def test_unknown_action_id_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="Unknown corporate-action ID",
    ):
        apply_action_decisions(
            statements=build_statements(),
            resolution=build_resolution(),
            decisions={
                "unknown-action": "APPROVE",
            },
        )


def test_approved_action_is_idempotent() -> None:
    resolution = build_resolution()
    action_id = get_action_id(resolution)

    decisions = {
        action_id: "APPROVE",
    }

    first_result = apply_action_decisions(
        statements=build_statements(),
        resolution=resolution,
        decisions=decisions,
    )

    second_result = apply_action_decisions(
        statements=build_statements(
            trades=first_result.combined_trades
        ),
        resolution=resolution,
        decisions=decisions,
    )

    assert second_result.summary[
        "applied_trade_count"
    ] == 0

    assert len(
        second_result.combined_trades
    ) == 1


def test_unresolved_issue_blocks_export() -> None:
    resolution = build_resolution(
        unresolved=True
    )

    action_id = get_action_id(resolution)

    result = apply_action_decisions(
        statements=build_statements(),
        resolution=resolution,
        decisions={
            action_id: "APPROVE",
        },
    )

    assert result.summary["status"] == (
        "REVIEW_REQUIRED"
    )

    assert result.summary[
        "unresolved_issue_count"
    ] == 1
