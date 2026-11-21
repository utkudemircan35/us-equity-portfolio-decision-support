from datetime import date

import pandas as pd
import pytest

import midas_portfolio_pipeline as pipeline
from midas_cash_evidence_service import (
    CASH_EVIDENCE_COLUMNS,
    CashAcquisitionEvidenceResult,
)
from midas_corporate_action_resolver import (
    RESOLUTION_COLUMNS,
    SPLIT_EVENT_COLUMNS,
    TICKER_ALIAS_COLUMNS,
    CorporateActionResolutionResult,
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
from midas_reconciliation_service import (
    RECONCILIATION_COLUMNS,
    MidasReconciliationResult,
)


def empty_frame(
    columns: list[str],
) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def build_trades() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": date(2022, 2, 7),
                "time": "10:00:00",
                "ticker": "TWTR",
                "side": "Buy",
                "shares": 0.221877,
                "price_usd": 40.0,
                "commission_usd": 0.0,
                "gross_amount_usd": 8.87508,
                "is_gift": False,
                "source_file": "February.pdf",
                "source_hash": "buy-source",
                "statement_start": date(2022, 2, 1),
                "statement_end": date(2022, 2, 28),
                "source_page": 1,
            }
        ],
        columns=TRADE_COLUMNS,
    )


def build_statements() -> CombinedMidasStatements:
    return CombinedMidasStatements(
        trades=build_trades(),
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
        statement_summaries=[
            {
                "source_file": "February.pdf",
            }
        ],
        duplicate_pdf_names=[],
        duplicates_removed={},
        ignored_order_count=0,
        warnings=["shared warning"],
    )


def build_reconciliation() -> (
    MidasReconciliationResult
):
    return MidasReconciliationResult(
        checks=empty_frame(
            RECONCILIATION_COLUMNS
        ),
        summary={},
        warnings=[
            "shared warning",
            "reconciliation warning",
        ],
    )


def build_corporate_actions() -> (
    CorporateActionResolutionResult
):
    return CorporateActionResolutionResult(
        checks=empty_frame(
            RESOLUTION_COLUMNS
        ),
        ticker_aliases=empty_frame(
            TICKER_ALIAS_COLUMNS
        ),
        split_events=empty_frame(
            SPLIT_EVENT_COLUMNS
        ),
        unresolved_issues=pd.DataFrame(),
        split_lookup_errors={},
        summary={},
        warnings=[],
    )


def build_cash_evidence() -> (
    CashAcquisitionEvidenceResult
):
    return CashAcquisitionEvidenceResult(
        evidence=empty_frame(
            CASH_EVIDENCE_COLUMNS
        ),
        best_candidates=empty_frame(
            CASH_EVIDENCE_COLUMNS
        ),
        summary={},
        warnings=[],
    )


def build_official_actions(
    with_candidate: bool = True,
    unresolved: bool = False,
) -> OfficialActionResolutionResult:
    if with_candidate:
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
                        "VERIFIED_BY_SEC"
                    ),
                    "expected_proceeds_usd": (
                        12.0257334
                    ),
                    "cash_evidence_amount_usd": (
                        None
                    ),
                    "cash_evidence_matches": False,
                    "source_url": (
                        "https://www.sec.gov/example"
                    ),
                }
            ],
            columns=SYNTHETIC_TRADE_COLUMNS,
        )

    else:
        preview = empty_frame(
            SYNTHETIC_TRADE_COLUMNS
        )

    unresolved_issues = (
        pd.DataFrame(
            [
                {
                    "ticker": "UNKNOWN",
                }
            ]
        )
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


def install_preview_mocks(
    monkeypatch,
    official_actions: (
        OfficialActionResolutionResult
    ),
) -> None:
    monkeypatch.setattr(
        pipeline,
        "load_midas_statements",
        lambda **kwargs: build_statements(),
    )

    monkeypatch.setattr(
        pipeline,
        "reconcile_midas_holdings",
        lambda **kwargs: (
            build_reconciliation()
        ),
    )

    monkeypatch.setattr(
        pipeline,
        "resolve_corporate_actions",
        lambda **kwargs: (
            build_corporate_actions()
        ),
    )

    monkeypatch.setattr(
        pipeline,
        "find_cash_evidence",
        lambda **kwargs: (
            build_cash_evidence()
        ),
    )

    monkeypatch.setattr(
        pipeline,
        "resolve_official_cash_actions",
        lambda **kwargs: official_actions,
    )


def test_prepare_pipeline_waits_for_confirmation(
    monkeypatch,
) -> None:
    install_preview_mocks(
        monkeypatch=monkeypatch,
        official_actions=(
            build_official_actions()
        ),
    )

    events = []

    preview = (
        pipeline.prepare_midas_portfolio_import(
            sources=[b"fake-pdf"],
            sec_user_agent=(
                "Test User test@example.com"
            ),
            progress_callback=events.append,
        )
    )

    assert preview.summary["status"] == (
        "AWAITING_USER_CONFIRMATION"
    )

    assert len(preview.action_review) == 1

    assert preview.action_review.iloc[0][
        "decision"
    ] == "PENDING"

    assert events[0].step == 1
    assert events[-1].step == 7
    assert events[-1].status == "completed"


def test_prepare_pipeline_deduplicates_warnings(
    monkeypatch,
) -> None:
    install_preview_mocks(
        monkeypatch=monkeypatch,
        official_actions=(
            build_official_actions(
                with_candidate=False
            )
        ),
    )

    preview = (
        pipeline.prepare_midas_portfolio_import(
            sources=[b"fake-pdf"],
        )
    )

    assert preview.summary["status"] == (
        "READY_FOR_FINALIZATION"
    )

    assert preview.warnings == [
        "shared warning",
        "reconciliation warning",
    ]


def test_prepare_pipeline_reports_unresolved_issue(
    monkeypatch,
) -> None:
    install_preview_mocks(
        monkeypatch=monkeypatch,
        official_actions=(
            build_official_actions(
                unresolved=True
            )
        ),
    )

    preview = (
        pipeline.prepare_midas_portfolio_import(
            sources=[b"fake-pdf"],
        )
    )

    assert preview.summary["status"] == (
        "REVIEW_REQUIRED"
    )

    assert preview.summary[
        "unresolved_issue_count"
    ] == 1


def build_preview_for_finalization(
    unresolved: bool = False,
) -> pipeline.MidasPortfolioPreview:
    official_actions = build_official_actions(
        unresolved=unresolved
    )

    action_review = (
        pipeline.prepare_action_review(
            official_actions
        )
    )

    return pipeline.MidasPortfolioPreview(
        statements=build_statements(),
        reconciliation=build_reconciliation(),
        corporate_actions=(
            build_corporate_actions()
        ),
        cash_evidence=build_cash_evidence(),
        official_actions=official_actions,
        action_review=action_review,
        summary={
            "statement_count": 1,
        },
        warnings=[],
    )


def test_finalize_pipeline_creates_workbook() -> None:
    preview = build_preview_for_finalization()

    action_id = str(
        preview.action_review.iloc[0][
            "action_id"
        ]
    )

    events = []

    result = (
        pipeline.finalize_midas_portfolio_import(
            preview=preview,
            decisions={
                action_id: "APPROVE",
            },
            progress_callback=events.append,
        )
    )

    assert result.summary["status"] == (
        "COMPLETED"
    )

    assert result.summary["trade_count"] == 2

    assert result.summary[
        "approved_action_count"
    ] == 1

    assert len(
        result.workbook.workbook_bytes
    ) > 0

    assert events[0].step == 8
    assert events[-1].step == 9
    assert events[-1].progress == 1.0


def test_finalize_requires_every_decision() -> None:
    preview = build_preview_for_finalization()

    with pytest.raises(
        ValueError,
        match="Every corporate action",
    ):
        pipeline.finalize_midas_portfolio_import(
            preview=preview,
            decisions={},
        )


def test_finalize_blocks_unresolved_issue() -> None:
    preview = build_preview_for_finalization(
        unresolved=True
    )

    action_id = str(
        preview.action_review.iloc[0][
            "action_id"
        ]
    )

    with pytest.raises(
        ValueError,
        match="cannot be finalized",
    ):
        pipeline.finalize_midas_portfolio_import(
            preview=preview,
            decisions={
                action_id: "APPROVE",
            },
        )
