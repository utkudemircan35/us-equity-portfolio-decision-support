from pathlib import Path
from types import SimpleNamespace

import pytest

import midas_pipeline_cli
import portfolio_pipeline


def test_cli_builds_explicit_sec_user_agent() -> None:
    result = (
        midas_pipeline_cli.resolve_cli_sec_user_agent(
            contact_email="owner@example.com",
            input_function=lambda prompt: pytest.fail(
                "Prompt should not be used."
            ),
        )
    )

    assert result == (
        "US Equity Portfolio Decision Support "
        "owner@example.com"
    )


def test_cli_uses_environment_sec_user_agent(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "SEC_USER_AGENT",
        "Portfolio App operator@example.com",
    )

    result = (
        midas_pipeline_cli.resolve_cli_sec_user_agent(
            contact_email=None,
            input_function=lambda prompt: pytest.fail(
                "Prompt should not be used."
            ),
        )
    )

    assert result == (
        "Portfolio App operator@example.com"
    )


def test_cli_prompts_for_sec_email_when_missing(
    monkeypatch,
) -> None:
    monkeypatch.delenv(
        "SEC_USER_AGENT",
        raising=False,
    )

    prompts: list[str] = []

    def provide_email(prompt: str) -> str:
        prompts.append(prompt)
        return "owner@example.com"

    result = (
        midas_pipeline_cli.resolve_cli_sec_user_agent(
            contact_email=None,
            input_function=provide_email,
        )
    )

    assert prompts == ["SEC contact email: "]
    assert result.endswith("owner@example.com")


def test_cli_rejects_invalid_prompted_email(
    monkeypatch,
) -> None:
    monkeypatch.delenv(
        "SEC_USER_AGENT",
        raising=False,
    )

    with pytest.raises(
        ValueError,
        match="invalid",
    ):
        midas_pipeline_cli.resolve_cli_sec_user_agent(
            contact_email=None,
            input_function=lambda prompt: "not-an-email",
        )


def test_portfolio_pipeline_selects_excel_source(
    tmp_path,
    monkeypatch,
) -> None:
    excel_path = tmp_path / "portfolio.xlsx"
    calls = {}

    def fail_midas_pipeline(**kwargs):
        pytest.fail(
            "Midas pipeline must not run for "
            "an Excel source."
        )

    def fake_portfolio_pipeline(**kwargs):
        calls["portfolio"] = kwargs

        return SimpleNamespace(
            database_path=tmp_path / "portfolio.db",
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        portfolio_pipeline,
        "run_midas_pipeline",
        fail_midas_pipeline,
    )

    monkeypatch.setattr(
        portfolio_pipeline,
        "run_portfolio_pipeline",
        fake_portfolio_pipeline,
    )

    result = (
        portfolio_pipeline
        .run_selected_portfolio_source(
            source="excel",
            excel_path=excel_path,
            replace_existing=True,
            progress_callback=None,
        )
    )

    assert result.database_path == (
        tmp_path / "portfolio.db"
    )

    assert calls["portfolio"] == {
        "excel_path": excel_path,
        "replace_existing": True,
        "progress_callback": None,
    }


def test_portfolio_pipeline_selects_midas_source(
    tmp_path,
    monkeypatch,
) -> None:
    midas_folder = tmp_path / "midas_ekstreleri"
    midas_output = tmp_path / "generated.xlsx"
    calls = {}

    def fake_midas_pipeline(**kwargs):
        calls["midas"] = kwargs

    def fake_portfolio_pipeline(**kwargs):
        calls["portfolio"] = kwargs

        return SimpleNamespace(
            database_path=tmp_path / "portfolio.db",
            elapsed_seconds=2.0,
        )

    monkeypatch.setattr(
        portfolio_pipeline,
        "run_midas_pipeline",
        fake_midas_pipeline,
    )

    monkeypatch.setattr(
        portfolio_pipeline,
        "run_portfolio_pipeline",
        fake_portfolio_pipeline,
    )

    portfolio_pipeline.run_selected_portfolio_source(
        source="midas",
        midas_folder=midas_folder,
        midas_output=midas_output,
        sec_contact_email="owner@example.com",
        overwrite_midas_output=True,
        replace_existing=True,
        progress_callback=None,
    )

    assert calls["midas"] == {
        "folder": midas_folder.resolve(),
        "output": midas_output.resolve(),
        "sec_contact_email": "owner@example.com",
        "approve_all": False,
        "reject_all": False,
        "overwrite": True,
    }

    assert calls["portfolio"] == {
        "excel_path": midas_output.resolve(),
        "replace_existing": True,
        "progress_callback": None,
    }


def test_midas_source_rejects_excel_path() -> None:
    with pytest.raises(
        ValueError,
        match="Do not provide an Excel path",
    ):
        (
            portfolio_pipeline
            .run_selected_portfolio_source(
                source="midas",
                excel_path=Path("portfolio.xlsx"),
            )
        )


def test_unknown_portfolio_source_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="either 'excel' or 'midas'",
    ):
        (
            portfolio_pipeline
            .run_selected_portfolio_source(
                source="unknown",
            )
        )
