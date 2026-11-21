from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Any

import pandas as pd

from midas_action_confirmation_service import (
    ActionConfirmationResult,
    apply_action_decisions,
    prepare_action_review,
)
from midas_cash_evidence_service import (
    CashAcquisitionEvidenceResult,
    find_cash_evidence,
)
from midas_corporate_action_resolver import (
    CorporateActionResolutionResult,
    resolve_corporate_actions,
)
from midas_import_service import (
    CombinedMidasStatements,
    MidasLoadProgress,
    MidasStatementInput,
    load_midas_statements,
)
from midas_official_action_resolver import (
    OfficialActionResolutionResult,
    resolve_official_cash_actions,
)
from midas_pdf_parser import PdfSource
from midas_portfolio_exporter import (
    MidasPortfolioWorkbook,
    build_midas_portfolio_workbook,
)
from midas_reconciliation_service import (
    MidasReconciliationResult,
    reconcile_midas_holdings,
)


TOTAL_PIPELINE_STEPS = 9


@dataclass(frozen=True)
class MidasPipelineProgress:
    """
    One frontend-friendly pipeline progress event.
    """

    step: int
    total_steps: int
    stage: str
    status: str
    message: str
    progress: float


MidasPipelineProgressCallback = Callable[
    [MidasPipelineProgress],
    None,
]


@dataclass
class MidasPortfolioPreview:
    """
    Analysis state retained while awaiting user decisions.
    """

    statements: CombinedMidasStatements
    reconciliation: MidasReconciliationResult

    corporate_actions: (
        CorporateActionResolutionResult
    )

    cash_evidence: CashAcquisitionEvidenceResult

    official_actions: (
        OfficialActionResolutionResult
    )

    action_review: pd.DataFrame
    summary: dict[str, Any]
    warnings: list[str]


@dataclass
class MidasPortfolioPipelineResult:
    """
    Final confirmed portfolio workbook and audit details.
    """

    preview: MidasPortfolioPreview

    confirmation: ActionConfirmationResult

    workbook: MidasPortfolioWorkbook

    summary: dict[str, Any]
    warnings: list[str]


def emit_pipeline_progress(
    callback: MidasPipelineProgressCallback | None,
    step: int,
    stage: str,
    status: str,
    message: str,
    progress: float | None = None,
) -> None:
    """
    Send one progress event to terminal or frontend.
    """

    if callback is None:
        return

    resolved_progress = (
        progress
        if progress is not None
        else step / TOTAL_PIPELINE_STEPS
    )

    callback(
        MidasPipelineProgress(
            step=step,
            total_steps=TOTAL_PIPELINE_STEPS,
            stage=stage,
            status=status,
            message=message,
            progress=max(
                0.0,
                min(
                    1.0,
                    resolved_progress,
                ),
            ),
        )
    )


def console_pipeline_progress(
    event: MidasPipelineProgress,
) -> None:
    """
    Print pipeline progress while testing locally.
    """

    print(
        f"[{event.step}/{event.total_steps}] "
        f"{event.message}"
    )


def unique_warnings(
    *warning_groups: list[str],
) -> list[str]:
    """
    Combine warnings without changing their first-seen order.
    """

    combined: list[str] = []

    for warning_group in warning_groups:
        for warning in warning_group:
            normalized = str(
                warning
            ).strip()

            if (
                normalized
                and normalized not in combined
            ):
                combined.append(normalized)

    return combined


def prepare_midas_portfolio_import(
    sources: Sequence[
        PdfSource | MidasStatementInput
    ],
    sec_user_agent: str | None = None,
    progress_callback: (
        MidasPipelineProgressCallback | None
    ) = None,
) -> MidasPortfolioPreview:
    """
    Parse and analyse Midas statements without applying
    proposed corporate-action trades.
    """

    emit_pipeline_progress(
        callback=progress_callback,
        step=1,
        stage="statements",
        status="started",
        message="Reading Midas statements...",
        progress=0.01,
    )

    def statement_progress(
        event: MidasLoadProgress,
    ) -> None:
        emit_pipeline_progress(
            callback=progress_callback,
            step=1,
            stage="statements",
            status=event.status,
            message=(
                f"{event.source_name}: "
                f"{event.message}"
            ),
            progress=(
                0.01
                + event.progress * 0.20
            ),
        )

    statements = load_midas_statements(
        sources=sources,
        progress_callback=statement_progress,
    )

    emit_pipeline_progress(
        callback=progress_callback,
        step=2,
        stage="reconciliation",
        status="started",
        message=(
            "Comparing transactions with "
            "monthly holdings..."
        ),
    )

    reconciliation = reconcile_midas_holdings(
        statements=statements
    )

    emit_pipeline_progress(
        callback=progress_callback,
        step=3,
        stage="corporate_actions",
        status="started",
        message=(
            "Checking splits and ticker changes..."
        ),
    )

    corporate_actions = resolve_corporate_actions(
        statements=statements,
        reconciliation=reconciliation,
    )

    emit_pipeline_progress(
        callback=progress_callback,
        step=4,
        stage="cash_evidence",
        status="started",
        message=(
            "Checking cash-acquisition evidence..."
        ),
    )

    cash_evidence = find_cash_evidence(
        statements=statements,
        corporate_actions=corporate_actions,
    )

    emit_pipeline_progress(
        callback=progress_callback,
        step=5,
        stage="official_actions",
        status="started",
        message=(
            "Checking official SEC documents..."
        ),
    )

    official_actions = (
        resolve_official_cash_actions(
            statements=statements,
            corporate_actions=(
                corporate_actions
            ),
            cash_evidence=cash_evidence,
            sec_user_agent=sec_user_agent,
        )
    )

    emit_pipeline_progress(
        callback=progress_callback,
        step=6,
        stage="action_review",
        status="started",
        message=(
            "Preparing corporate actions "
            "for user review..."
        ),
    )

    action_review = prepare_action_review(
        official_actions
    )

    unresolved_count = len(
        official_actions.unresolved_issues
    )

    if unresolved_count:
        pipeline_status = "REVIEW_REQUIRED"

    elif not action_review.empty:
        pipeline_status = (
            "AWAITING_USER_CONFIRMATION"
        )

    else:
        pipeline_status = (
            "READY_FOR_FINALIZATION"
        )

    warnings = unique_warnings(
        statements.warnings,
        reconciliation.warnings,
        corporate_actions.warnings,
        cash_evidence.warnings,
        official_actions.warnings,
    )

    summary = {
        "status": pipeline_status,
        "statement_count": len(
            statements.statement_summaries
        ),
        "trade_count": len(
            statements.trades
        ),
        "cash_movement_count": len(
            statements.cash_movements
        ),
        "dividend_count": len(
            statements.dividends
        ),
        "reconciliation_issue_count": len(
            reconciliation.issues
        ),
        "corporate_action_candidate_count": (
            len(action_review)
        ),
        "unresolved_issue_count": (
            unresolved_count
        ),
        "warning_count": len(warnings),
    }

    emit_pipeline_progress(
        callback=progress_callback,
        step=7,
        stage="preview",
        status="completed",
        message=(
            "Midas import preview is ready."
        ),
    )

    return MidasPortfolioPreview(
        statements=statements,
        reconciliation=reconciliation,
        corporate_actions=corporate_actions,
        cash_evidence=cash_evidence,
        official_actions=official_actions,
        action_review=action_review,
        summary=summary,
        warnings=warnings,
    )


def finalize_midas_portfolio_import(
    preview: MidasPortfolioPreview,
    decisions: Mapping[str, str],
    progress_callback: (
        MidasPipelineProgressCallback | None
    ) = None,
) -> MidasPortfolioPipelineResult:
    """
    Apply explicit decisions and build portfolio.xlsx bytes.
    """

    emit_pipeline_progress(
        callback=progress_callback,
        step=8,
        stage="confirmation",
        status="started",
        message=(
            "Applying user decisions..."
        ),
    )

    confirmation = apply_action_decisions(
        statements=preview.statements,
        resolution=preview.official_actions,
        decisions=decisions,
        require_all_decisions=True,
    )

    if (
        confirmation.summary.get("status")
        != "READY_FOR_EXPORT"
    ):
        raise ValueError(
            "Midas import cannot be finalized. "
            "Corporate-action review status: "
            f"{confirmation.summary.get('status')}"
        )

    emit_pipeline_progress(
        callback=progress_callback,
        step=9,
        stage="workbook",
        status="started",
        message=(
            "Creating portfolio.xlsx..."
        ),
    )

    workbook = build_midas_portfolio_workbook(
        statements=preview.statements,
        confirmation=confirmation,
        ticker_aliases=(
            preview
            .corporate_actions
            .ticker_aliases
        ),
    )

    summary = {
        "status": "COMPLETED",
        "statement_count": (
            preview.summary[
                "statement_count"
            ]
        ),
        "trade_count": len(
            workbook.trades
        ),
        "cash_movement_count": len(
            workbook.cash_movements
        ),
        "dividend_count": len(
            workbook.dividends
        ),
        "approved_action_count": (
            confirmation.summary[
                "approved_count"
            ]
        ),
        "rejected_action_count": (
            confirmation.summary[
                "rejected_count"
            ]
        ),
        "workbook_size_bytes": len(
            workbook.workbook_bytes
        ),
    }

    emit_pipeline_progress(
        callback=progress_callback,
        step=9,
        stage="workbook",
        status="completed",
        message=(
            "portfolio.xlsx is ready for download."
        ),
        progress=1.0,
    )

    return MidasPortfolioPipelineResult(
        preview=preview,
        confirmation=confirmation,
        workbook=workbook,
        summary=summary,
        warnings=preview.warnings.copy(),
    )
