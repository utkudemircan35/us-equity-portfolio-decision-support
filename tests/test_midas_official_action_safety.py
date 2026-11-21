from datetime import date

import pandas as pd
import pytest

import midas_official_action_resolver as resolver
from midas_cash_evidence_service import (
    CashAcquisitionEvidenceResult,
)
from midas_corporate_action_resolver import (
    CorporateActionResolutionResult,
)
from midas_import_service import (
    CombinedMidasStatements,
)
from midas_pdf_parser import (
    CASH_MOVEMENT_COLUMNS,
    DIVIDEND_COLUMNS,
    HOLDING_COLUMNS,
    TRADE_COLUMNS,
    UNSUPPORTED_ACTIVITY_COLUMNS,
)
from sec_corporate_action_service import (
    SecCashAcquisitionResult,
    SecDocumentEvidence,
    SecSearchHit,
    select_cash_price_evidence,
)


def build_sec_evidence(
    price: float,
    document_url: str,
    score: float = 10.0,
) -> SecDocumentEvidence:
    hit = SecSearchHit(
        company_name="Twitter, Inc.",
        cik="1418091",
        form="SC 13D/A",
        filing_date=date(2022, 10, 28),
        accession_number="test",
        filename="test.htm",
        document_url=document_url,
    )

    return SecDocumentEvidence(
        hit=hit,
        cash_price_per_share=price,
        event_date=date(2022, 10, 28),
        evidence_score=score,
        context="cash merger per share",
    )


def test_cash_implied_price_beats_repeated_wrong_price() -> None:
    evidence = [
        build_sec_evidence(
            2.80,
            "https://www.sec.gov/wrong-1",
        ),
        build_sec_evidence(
            2.80,
            "https://www.sec.gov/wrong-2",
        ),
        build_sec_evidence(
            2.80,
            "https://www.sec.gov/wrong-3",
        ),
        build_sec_evidence(
            54.20,
            "https://www.sec.gov/correct",
        ),
    ]

    selected_price, selected_evidence = (
        select_cash_price_evidence(
            all_evidence=evidence,
            expected_price_per_share=54.21912,
        )
    )

    assert selected_price == pytest.approx(
        54.20
    )
    assert len(selected_evidence) == 1


def build_statements() -> CombinedMidasStatements:
    holdings = pd.DataFrame(
        [
            {
                "statement_date": date(
                    2022,
                    9,
                    30,
                ),
                "ticker": "TWTR",
                "company_name": "Twitter, Inc.",
                "shares": 0.221877,
                "average_cost_usd": 40.0,
                "profit_loss_usd": 0.0,
                "market_value_usd": 10.0,
                "source_file": "September.pdf",
                "source_hash": "holdings-source",
                "statement_start": date(
                    2022,
                    9,
                    1,
                ),
                "statement_end": date(
                    2022,
                    9,
                    30,
                ),
                "source_page": 1,
            }
        ],
        columns=HOLDING_COLUMNS,
    )

    return CombinedMidasStatements(
        trades=pd.DataFrame(
            columns=TRADE_COLUMNS
        ),
        cash_movements=pd.DataFrame(
            columns=CASH_MOVEMENT_COLUMNS
        ),
        dividends=pd.DataFrame(
            columns=DIVIDEND_COLUMNS
        ),
        holdings=holdings,
        unsupported_activities=pd.DataFrame(
            columns=UNSUPPORTED_ACTIVITY_COLUMNS
        ),
        statement_summaries=[],
        duplicate_pdf_names=[],
        duplicates_removed={},
        ignored_order_count=0,
        warnings=[],
    )


def build_corporate_actions() -> (
    CorporateActionResolutionResult
):
    unresolved = pd.DataFrame(
        [
            {
                "previous_statement_date": date(
                    2022,
                    9,
                    30,
                ),
                "statement_date": date(
                    2022,
                    10,
                    31,
                ),
                "ticker": "TWTR",
                "previous_shares": 0.221877,
                "observed_shares": 0.0,
                "split_adjusted_difference": (
                    -0.221877
                ),
            }
        ]
    )

    return CorporateActionResolutionResult(
        checks=pd.DataFrame(),
        ticker_aliases=pd.DataFrame(),
        split_events=pd.DataFrame(),
        unresolved_issues=unresolved,
        split_lookup_errors={},
        summary={},
        warnings=[],
    )


def build_cash_evidence() -> (
    CashAcquisitionEvidenceResult
):
    candidate = pd.DataFrame(
        [
            {
                "ticker": "TWTR",
                "cash_amount_usd": 12.03,
                "implied_price_per_share": (
                    12.03 / 0.221877
                ),
            }
        ]
    )

    return CashAcquisitionEvidenceResult(
        evidence=candidate.copy(),
        best_candidates=candidate,
        summary={},
        warnings=[],
    )


def build_sec_result(
    price: float,
    event_date: date,
) -> SecCashAcquisitionResult:
    return SecCashAcquisitionResult(
        status="FOUND",
        ticker="TWTR",
        company_query="Twitter",
        cash_price_per_share=price,
        event_date=event_date,
        supporting_document_count=1,
        best_document_url=None,
        best_document_form="SC 13D/A",
        best_document_filing_date=event_date,
        evidence=[],
        warnings=[],
    )


def test_cash_mismatch_is_blocked(
    monkeypatch,
) -> None:
    received_expected_price = {}

    def fake_sec_lookup(**kwargs):
        received_expected_price["value"] = (
            kwargs["expected_price_per_share"]
        )

        return build_sec_result(
            price=2.80,
            event_date=date(2022, 7, 25),
        )

    monkeypatch.setattr(
        resolver,
        "find_sec_cash_acquisition",
        fake_sec_lookup,
    )

    result = resolver.resolve_official_cash_actions(
        statements=build_statements(),
        corporate_actions=(
            build_corporate_actions()
        ),
        cash_evidence=build_cash_evidence(),
    )

    assert received_expected_price[
        "value"
    ] == pytest.approx(
        12.03 / 0.221877
    )

    assert result.official_actions.empty
    assert result.synthetic_trades_preview.empty
    assert len(result.unresolved_issues) == 1

    assert result.unresolved_issues.iloc[0][
        "official_lookup_status"
    ] == "CASH_EVIDENCE_MISMATCH"


def test_matching_sec_price_is_allowed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        resolver,
        "find_sec_cash_acquisition",
        lambda **kwargs: build_sec_result(
            price=54.20,
            event_date=date(2022, 10, 28),
        ),
    )

    result = resolver.resolve_official_cash_actions(
        statements=build_statements(),
        corporate_actions=(
            build_corporate_actions()
        ),
        cash_evidence=build_cash_evidence(),
    )

    assert result.unresolved_issues.empty
    assert len(result.official_actions) == 1
    assert len(
        result.synthetic_trades_preview
    ) == 1

    action = result.official_actions.iloc[0]

    assert action[
        "cash_price_per_share"
    ] == pytest.approx(54.20)
    assert action[
        "rounded_proceeds_usd"
    ] == pytest.approx(12.03)
    assert action[
        "cash_evidence_matches"
    ] == True
