# Frontend Implementation Handoff

## 1. Purpose of this document

This document is an implementation contract for an AI coding agent that will build the Streamlit frontend for this repository.

The human collaborators may not know the backend internals. The AI agent must inspect the repository, follow this contract, implement the frontend in small verified steps, and explain each step to the collaborators in plain language.

Repository:

- Remote: `https://github.com/utkudemircan35/us-equity-portfolio-decision-support.git`
- Earlier Excel-only backend tag: `backend-v1.0`
- Default branch: `main`
- Current backend test baseline: `70 passed`
- Portfolio validation baseline: `11 PASS, 0 FAIL`
- Python used for the verified backend: `3.14.0`

If this document conflicts with the current Python source or tests, the current source and tests are the final authority. Report the discrepancy before changing backend behavior.

## 2. Product objective

Build a local, single-user Streamlit decision-support application for a USD-denominated portfolio containing US-listed equities, ETFs and ADRs.

The frontend must let the user:

1. Choose an input type: upload a complete portfolio Excel workbook or upload multiple Midas monthly statement PDFs.
2. For Midas imports, inspect warnings and proposed corporate actions, then explicitly approve or reject every candidate before portfolio data is replaced.
3. View portfolio value, cash, holdings, profit/loss, allocation and TWR history.
4. Add, edit and delete trades, cash movements and dividends.
5. Inspect individual security history and performance.
6. Run portfolio risk analysis.
7. Run portfolio optimization with user-selected tickers and constraints.
8. Generate a rebalancing plan.
9. Inspect backend health and validation results.

The application is a decision-support tool. It must not present results as guaranteed returns, investment advice or executable brokerage orders.

## 3. Non-negotiable architecture rules

These rules override frontend convenience:

1. **Do not reimplement financial calculations in Streamlit.**
   - No duplicate TWR, P&L, holdings, cash, risk, optimization or rebalancing formulas.
   - Call the existing backend services and display their results.

2. **Do not write directly to SQLite from frontend code.**
   - Use `transaction_service.py` for mutations.
   - Use `run_portfolio_pipeline()` for the common eight-stage import after a valid workbook exists.
   - Use `prepare_midas_portfolio_import()` and `finalize_midas_portfolio_import()` for the two-phase Midas review workflow.
   - Use `dashboard_service.py` for dashboard reads.

3. **Do not manually adjust historical trades for stock splits.**
   - Trade forms accept raw broker-reported shares and raw broker-reported prices.
   - The backend downloads split history and calculates adjusted values.

4. **Do not round stored or calculated values.**
   - Round only for visual display.
   - Preserve original values when passing data back to edit functions.

5. **Always refresh calculated tables after a mutation.**
   - Frontend calls must use `refresh_calculated_data=True`.
   - Do not expose a user control that disables refresh.

6. **Do not bypass snapshot rollback.**
   - All add/edit/delete operations must use the public transaction service.
   - A failed mutation automatically restores the previous database.

7. **Treat validation failure as a blocking state.**
   - Do not show stale results as healthy after a failed pipeline or mutation.
   - Display the error and the validation report.

8. **Do not add Monte Carlo simulation, tax calculation, FX conversion, brokerage execution, authentication or cloud multi-tenancy in v1.**

9. **Do not commit personal data.**
   - `portfolio.xlsx`, `portfolio.db`, statements, environment files and secrets remain Git-ignored.

10. **Do not modify backend behavior merely to simplify UI code.**
    - If a backend change appears necessary, first prove it with a failing test and obtain explicit approval.

11. **Do not call terminal input functions from Streamlit.**
    - `midas_pipeline_cli.py` is for local terminal use and contains `input()` prompts.
    - Streamlit must render its own SEC email field and corporate-action decision controls, then call the frontend-friendly Midas pipeline functions directly.

12. **Never auto-approve a corporate action in the UI.**
    - Every proposed synthetic trade requires an explicit user decision.
    - `REVIEW_REQUIRED` is blocking and cannot be bypassed by a frontend toggle.

## 4. Operating model and limitations

The current backend uses one SQLite file at the project root. It is suitable for a local single-user application.

It is not safe as a public multi-user deployment because different users would share the same database. Public hosting requires user authentication and per-user database isolation, which is outside the current scope.

Market data and metadata come from Yahoo Finance through `yfinance`. Network operations may be slow or temporarily fail. The UI must show progress and must not imply that the application has frozen.

Delisted securities are handled generically by backend logic. Do not hard-code `TWTR` or any other ticker in frontend code. The backend may use trade-price fallback when Yahoo Finance cannot return required historical prices.

## 5. Repository map

Important backend files:

- `backend/database.py`: connection helpers, database path and schema validation.
- `backend/database_setup.py`: tables, indexes and migrations.
- `backend/excel_importer.py`: Excel validation and source-table import.
- `backend/asset_metadata.py`: asset metadata download.
- `backend/market_data.py`: market-price download and delisted fallback routing.
- `backend/portfolio_calculator.py`: holdings, cash, cost basis and P&L.
- `backend/portfolio_daily_calculator.py`: daily portfolio value and portfolio TWR.
- `backend/security_daily_calculator.py`: daily security valuation and security TWR.
- `backend/performance_calculator.py`: security performance derived from `security_daily`.
- `backend/risk_calculator.py`: portfolio and security risk metrics.
- `backend/portfolio_optimizer.py`: minimum variance, maximum Sharpe and efficient frontier.
- `backend/rebalancing_calculator.py`: target-vs-current trade plan.
- `backend/portfolio_pipeline.py`: single terminal entry point for Excel/Midas selection and the common eight-stage workbook pipeline.
- `backend/midas_pdf_parser.py`: parses one Midas PDF.
- `backend/midas_import_service.py`: combines uploaded statements and removes duplicate PDFs/records.
- `backend/midas_reconciliation_service.py`: compares transaction-derived shares with monthly holdings snapshots.
- `backend/midas_corporate_action_resolver.py`: checks splits and verified ticker changes.
- `backend/midas_cash_evidence_service.py`: finds Midas cash evidence for disappearing positions.
- `backend/sec_corporate_action_service.py`: retrieves official SEC evidence.
- `backend/midas_official_action_resolver.py`: combines official evidence with local cash and holdings evidence.
- `backend/midas_action_confirmation_service.py`: applies explicit approve/reject decisions.
- `backend/midas_portfolio_exporter.py`: creates a `portfolio.xlsx`-compatible workbook.
- `backend/midas_portfolio_pipeline.py`: two-phase frontend-friendly Midas preview/finalization API.
- `backend/midas_pipeline_cli.py`: terminal-only Midas interaction; do not use it for Streamlit widgets.
- `backend/portfolio_refresh_service.py`: six-stage refresh after CRUD changes.
- `backend/transaction_service.py`: safe CRUD API with snapshot rollback.
- `backend/dashboard_service.py`: read API intended for the frontend.
- `backend/portfolio_validation_service.py`: structural and financial consistency checks.

Tests:

- `tests/test_transaction_service.py`
- `tests/test_backend_integration.py`
- `tests/test_excel_importer.py`
- `tests/test_midas_import.py`
- `tests/test_midas_action_confirmation.py`
- `tests/test_midas_official_action_safety.py`
- `tests/test_midas_portfolio_exporter.py`
- `tests/test_midas_portfolio_pipeline.py`
- `tests/test_midas_pipeline_cli.py`
- `tests/conftest.py`

## 6. Frontend project structure

Create frontend files without moving or renaming backend files:

```text
Project Folder/
├── backend/
├── frontend/
│   ├── app.py
│   ├── bootstrap.py
│   ├── state.py
│   ├── formatting.py
│   ├── progress.py
│   ├── components/
│   │   ├── navigation.py
│   │   ├── metrics.py
│   │   ├── charts.py
│   │   ├── tables.py
│   │   └── errors.py
│   └── pages/
│       ├── overview.py
│       ├── transactions.py
│       ├── security_analysis.py
│       ├── risk.py
│       ├── optimization.py
│       ├── rebalancing.py
│       └── system_health.py
├── tests/
│   └── test_frontend_smoke.py
├── FRONTEND_HANDOFF.md
├── README.md
└── requirements.txt
```

Exact file names may be adjusted to the Streamlit version being used, but keep frontend code isolated under `frontend/`.

## 7. Bootstrapping imports

The backend directory is not currently installed as a Python package. The frontend must add it to `sys.path` once, centrally, before backend imports.

Example for `frontend/bootstrap.py`:

```python
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
```

Do not repeat this logic in every page. Import `frontend.bootstrap` before service imports or provide one shared initialization function.

Add `streamlit` to `requirements.txt` using a version verified with Python 3.14. Keep existing pinned dependencies unless compatibility testing proves a change is required.

Run the app from the project root. The final command should be documented in README, normally:

```bash
streamlit run frontend/app.py
```

## 8. Database model and ownership

Source-of-truth user tables:

- `trades`
- `cash_movements`
- `dividends`

Downloaded/reference tables:

- `assets`
- `market_prices`

Calculated tables:

- `holdings_summary`
- `portfolio_daily`
- `security_daily`

Never edit calculated or downloaded tables in frontend code.

Important ownership:

- Portfolio TWR source: `portfolio_daily.cumulative_twr`
- Security TWR source: `security_daily.cumulative_twr`
- Current holdings, cash, cost basis and P&L source: `holdings_summary`
- Raw trade input source: `trades.raw_shares` and `trades.raw_price_usd`
- Split-adjusted fields are derived: `shares`, `price_usd`, `split_factor`

## 9. Complete portfolio import workflows

The onboarding/import screen must begin with an explicit source selector:

- `Excel workbook`
- `Midas statements`

Only the controls for the selected source should be visible. Both paths must end by calling the same `run_portfolio_pipeline()` function with a temporary compatible workbook.

### 9.1 Excel upload workflow

The uploader accepts one `.xlsx` workbook containing at least these three required sheets. Extra sheets may be present and are ignored by the importer:

### `trades`

Required columns:

- `date`
- `time`
- `ticker`
- `side`
- `shares`
- `price_usd`
- `commission_usd`

Optional column:

- `is_gift`

### `cash_movements`

Required columns:

- `date`
- `time`
- `type`
- `amount_usd`

Allowed types: `DEPOSIT`, `WITHDRAWAL`, `INTEREST`.

### `dividends`

Required columns:

- `date`
- `time`
- `ticker`
- `net_dividend_usd`

Excel upload rules:

1. The workbook must contain complete portfolio history, not only new rows.
2. The user must not manually split-adjust shares or prices.
3. The UI must warn that replacement imports overwrite current source transactions.
4. Require an explicit confirmation checkbox before `replace_existing=True`.
5. Save uploaded bytes to a temporary `.xlsx` path.
6. Call `run_portfolio_pipeline()`.
7. Delete the temporary file in a `finally` block.
8. Clear all frontend read caches after success.
9. Display the final validation result.
10. On failure, show the backend error without claiming success.

Backend call:

```python
from pathlib import Path
from portfolio_pipeline import run_portfolio_pipeline

result = run_portfolio_pipeline(
    excel_path=Path(temp_xlsx_path),
    replace_existing=True,
    progress_callback=streamlit_progress_callback,
)
```

The complete pipeline has eight stages:

1. `database_setup` - Preparing database
2. `excel_import` - Importing portfolio transactions
3. `asset_metadata` - Loading asset information
4. `market_data` - Downloading market prices
5. `holdings_summary` - Calculating current holdings
6. `portfolio_daily` - Building daily portfolio history
7. `security_daily` - Building daily security history
8. `validation` - Validating portfolio results

### 9.2 Midas PDF upload workflow

The Midas uploader accepts multiple `.pdf` files in one submission. The user may upload statements in any filename order; backend code sorts and deduplicates them. Do not require the browser filename to follow a specific month format.

Frontend inputs:

- Multiple PDF uploader
- SEC contact email
- Explicit button to analyse statements
- Corporate-action review table when candidates exist
- One `Approve` or `Reject` decision for every candidate
- Explicit confirmation before replacing existing portfolio data

The SEC contact email identifies the application operator for SEC fair-access requests. It must not be stored in the workbook, SQLite database, logs or frontend state longer than needed for the import request.

Convert each uploaded file to `MidasStatementInput` using the uploaded bytes and original display name:

```python
from midas_import_service import MidasStatementInput

sources = [
    MidasStatementInput(
        source=uploaded_file.getvalue(),
        source_name=uploaded_file.name,
    )
    for uploaded_file in uploaded_files
]
```

#### Phase A - Analyse without mutating the portfolio

```python
from midas_portfolio_pipeline import (
    prepare_midas_portfolio_import,
)

sec_user_agent = (
    "US Equity Portfolio Decision Support "
    + sec_contact_email.strip()
)

preview = prepare_midas_portfolio_import(
    sources=sources,
    sec_user_agent=sec_user_agent,
    progress_callback=midas_progress_callback,
)
```

Store `preview` in `st.session_state` while the user reviews the result. Display:

- `preview.summary`
- `preview.warnings`
- `preview.action_review`
- `preview.official_actions.unresolved_issues` when present
- Duplicate statement and duplicate-record information from the preview/statements result

Status behavior:

- `REVIEW_REQUIRED`: block finalization and display unresolved issues. Do not offer an override button.
- `AWAITING_USER_CONFIRMATION`: render a decision control for every row in `preview.action_review`.
- `READY_FOR_FINALIZATION`: no candidate requires a decision; allow finalization with an empty decision mapping.

The action review must show at minimum:

- Action ID
- Date
- Ticker
- Shares
- Price per share
- Expected proceeds
- Matching Midas cash evidence
- Confidence/status

Never infer a decision from a checkbox default. Require an explicit value for every candidate.

#### Phase B - Apply decisions and create the compatible workbook

```python
from midas_portfolio_pipeline import (
    finalize_midas_portfolio_import,
)

result = finalize_midas_portfolio_import(
    preview=preview,
    decisions=decisions,
    progress_callback=midas_progress_callback,
)

workbook_bytes = result.workbook.workbook_bytes
```

`decisions` is a mapping from each `action_id` to exactly `APPROVE` or `REJECT`. The backend rejects missing or unknown decisions.

The generated workbook already contains the required `trades`, `cash_movements` and `dividends` sheets. It also applies verified ticker aliases and Yahoo-compatible class-share formatting. Do not alter or rebuild its rows in frontend code.

Write `workbook_bytes` to a temporary `.xlsx` file, then run the common eight-stage portfolio pipeline:

```python
result = run_portfolio_pipeline(
    excel_path=Path(temp_xlsx_path),
    replace_existing=True,
    progress_callback=streamlit_progress_callback,
)
```

Delete the temporary workbook in a `finally` block. Clear the retained preview, decisions, uploaded bytes and relevant read caches after success or when the user starts a new import.

### 9.3 Terminal workflow reference

The terminal user runs only `backend/portfolio_pipeline.py`:

```bash
# Excel
python backend/portfolio_pipeline.py \
    --source excel \
    portfolio.xlsx \
    --replace-existing

# Midas PDFs from Project Folder/midas_ekstreleri/
python backend/portfolio_pipeline.py \
    --source midas \
    --overwrite-midas-output \
    --replace-existing
```

This terminal behavior is a reference for business flow only. Streamlit must not launch the CLI as a subprocess or attempt to answer its `input()` prompts.

## 10. Progress callback contract

`portfolio_pipeline.PipelineProgress` contains:

- `stage_index: int`
- `total_stages: int`
- `stage_key: str`
- `stage_name: str`
- `status: str` with `started`, `completed` or `failed`
- `message: str`
- `progress: float` in the inclusive range 0.0 to 1.0

The callback must:

- Update a progress bar from `event.progress`.
- Display `event.stage_index / event.total_stages`.
- Display `event.stage_name` and `event.message`.
- Use a success state for `completed`.
- Use an error state for `failed`.
- Preserve a visible list of completed steps so the user can see activity.

Do not perform Streamlit UI updates from a background thread. The current backend is synchronous; call it synchronously inside the button action and update widgets through the callback.

CRUD refreshes use the same event type but contain six stages: metadata, market data, holdings, portfolio daily, security daily and validation.

`midas_portfolio_pipeline.MidasPipelineProgress` contains:

- `step: int`
- `total_steps: int`
- `stage: str`
- `status: str`
- `message: str`
- `progress: float`

The Midas callback covers statement reading, reconciliation, corporate-action checks, cash evidence, SEC evidence, review preparation, confirmation and workbook creation. The UI should keep these messages visible until the common eight-stage portfolio pipeline begins, then continue with the standard progress adapter.

## 11. Transaction service contract

Import public functions from `backend/transaction_service.py`. Never replace them with direct SQL.

### Trade reads

- `list_trades() -> pd.DataFrame`
- `get_trade(trade_id: int) -> dict`

Trade records include:

- `trade_id`
- `date`
- `time`
- `ticker`
- `side`
- `raw_shares`
- `raw_price_usd`
- `split_factor`
- `is_gift`
- `shares`
- `price_usd`
- `commission_usd`
- `net_flow_usd`
- `cumulative_shares`

Only raw fields and normal user inputs belong in edit forms. Derived fields are read-only.

### Trade mutations

- `add_trade(date, ticker, side, raw_shares, raw_price_usd, commission_usd=0.0, time_value=None, is_gift=False, refresh_calculated_data=True, progress_callback=...)`
- `edit_trade(trade_id, date, ticker, side, raw_shares, raw_price_usd, commission_usd=0.0, time_value=None, is_gift=False, refresh_calculated_data=True, progress_callback=...)`
- `delete_trade(trade_id, refresh_calculated_data=True, progress_callback=...)`

Trade form rules:

- Side is `Buy` or `Sell`.
- Raw shares must be greater than zero.
- Raw price must be greater than zero.
- Commission must be zero or greater.
- A gift must be a Buy.
- A gift cannot have commission.
- A gift has zero cash flow.
- A Sell cannot exceed available cumulative shares.
- Sell commission cannot exceed gross sale value.
- Historical trades are allowed; the backend recalculates later balances and splits.
- Same-date/time ordering is resolved by `trade_id`.

Deletion and ticker edits may invalidate later sales. Let backend validation reject and roll back the operation.

### Cash movement reads and mutations

- `list_cash_movements() -> pd.DataFrame`
- `get_cash_movement(cash_id: int) -> dict`
- `add_cash_movement(date, movement_type, amount_usd, time_value=None, refresh_calculated_data=True, progress_callback=...)`
- `edit_cash_movement(cash_id, date, movement_type, amount_usd, time_value=None, refresh_calculated_data=True, progress_callback=...)`
- `delete_cash_movement(cash_id, refresh_calculated_data=True, progress_callback=...)`

Allowed movement types:

- `DEPOSIT`
- `WITHDRAWAL`
- `INTEREST`

Amount must be greater than zero. The movement type controls cash direction; do not ask the user to enter negative values.

### Dividend reads and mutations

- `list_dividends() -> pd.DataFrame`
- `get_dividend(dividend_id: int) -> dict`
- `add_dividend(date, ticker, net_dividend_usd, time_value=None, refresh_calculated_data=True, progress_callback=...)`
- `edit_dividend(dividend_id, date, ticker, net_dividend_usd, time_value=None, refresh_calculated_data=True, progress_callback=...)`
- `delete_dividend(dividend_id, refresh_calculated_data=True, progress_callback=...)`

`net_dividend_usd` must be zero or greater.

### Mutation result and failure behavior

Mutations return `MutationResult`:

- `table_name: str`
- `operation: str`
- `record_id: int`
- `refresh_result: PortfolioRefreshResult | None`

The UI must:

1. Disable or guard against duplicate submission.
2. Show the six-stage progress callback.
3. Show success only after the complete refresh and validation pass.
4. Clear read caches after success.
5. Catch exceptions and display a concise user message plus expandable technical details.
6. State that the previous database was restored when the backend error says rollback occurred.
7. Require explicit confirmation for deletes.

## 12. Dashboard read service contract

Use `backend/dashboard_service.py` for screen data.

### System health

`get_portfolio_health() -> dict`

Returns:

- `is_valid: bool`
- `summary: dict`
- `checks: pd.DataFrame`

The summary contains status and passed/warning/failed/total counts. Show a green healthy state only when `is_valid` is true.

### Overview

`get_overview_dashboard_data(recent_activity_limit=20) -> dict`

Returns:

- `overview: dict`
- `holdings: pd.DataFrame`
- `ticker_allocation: pd.DataFrame`
- `sector_allocation: pd.DataFrame`
- `asset_type_allocation: pd.DataFrame`
- `portfolio_history: pd.DataFrame`
- `recent_activity: pd.DataFrame`

`overview` fields:

- `valuation_date`
- `holdings_as_of_date`
- `portfolio_value_usd`
- `securities_value_usd`
- `cash_balance_usd`
- `cash_weight`
- `realized_pnl_usd`
- `unrealized_pnl_usd`
- `total_pnl_usd`
- `open_position_count`
- `closed_position_count`
- `daily_return`
- `cumulative_twr`
- `net_external_capital_usd`
- `total_interest_usd`
- `total_dividend_usd`
- `reconciliation_difference_usd`

### Holdings

`get_holdings_table(status="OPEN", include_cash=True) -> pd.DataFrame`

Allowed status: `OPEN`, `CLOSED`, `ALL`.

Columns:

- `ticker`
- `company_name`
- `asset_type`
- `sector`
- `industry`
- `as_of_date`
- `status`
- `shares`
- `price_usd`
- `market_value_usd`
- `weight`
- `remaining_cost_basis_usd`
- `average_cost_usd`
- `realized_pnl_usd`
- `unrealized_pnl_usd`
- `total_pnl_usd`

### Allocation

`get_allocation_summary(group_by="ticker") -> pd.DataFrame`

Allowed groupings:

- `ticker`
- `asset_type`
- `sector`
- `industry`

Columns include `group_by`, `category`, `market_value_usd` and `weight`.

### Portfolio history

`get_portfolio_history(start_date=None, end_date=None) -> pd.DataFrame`

Columns:

- `date`
- `opening_value_usd`
- `securities_value_usd`
- `cash_balance_usd`
- `closing_value_usd`
- `external_inflow_usd`
- `external_outflow_usd`
- `daily_return`
- `cumulative_twr`

The first real portfolio event defines the portfolio calendar. Do not infer a new start date from available market prices.

### Recent activity

`get_recent_activity(limit=20) -> pd.DataFrame`

Contains combined trade, cash and dividend events with:

- `activity_id`
- `date`
- `time`
- `category`
- `event_type`
- `ticker`
- `quantity`
- `price_usd`
- `commission_usd`
- `cash_flow_usd`

### Security history

`get_available_security_tickers() -> pd.DataFrame`

Provides ticker metadata, position status, history dates and observation count.

`get_security_history(ticker, start_date=None, end_date=None) -> pd.DataFrame`

Columns:

- `key`
- `ticker`
- `date`
- `opening_shares`
- `closing_shares`
- `close_usd`
- `opening_value_usd`
- `closing_value_usd`
- `trade_flow_usd`
- `dividend_usd`
- `daily_return`
- `cumulative_twr`

`get_security_performance(risk_free_rate=None) -> dict` returns:

- `risk_free_rate`: resolved source, observation date and rate details
- `performance`: security performance DataFrame

Do not calculate security TWR from trade rows in frontend code. Display `security_daily`-derived results returned by the service.

### Risk

`get_risk_dashboard_data(start_date=None, end_date=None, risk_free_rate=None) -> dict`

Returns:

- `summary`
- `security_metrics`
- `correlation_matrix`
- `covariance_matrix`
- `risk_contribution`

The default risk-free rate is resolved from Yahoo Finance ticker `^TNX`, representing the US 10-year Treasury yield. Always show:

- Risk-free-rate source
- Observation date
- Annual rate used

If a manual risk-free-rate control is provided, label it as an annual decimal. Example: `0.04` means 4%.

Risk outputs may include TWR, annualized return, volatility, Sharpe ratio, Sortino ratio, maximum drawdown, historical VaR, historical CVaR, correlations, covariance and risk contribution. Use the backend-provided labels and values.

### Optimization

`get_optimization_dashboard_data(...)` arguments:

- `lookback_years=3`
- `minimum_observations=252`
- `min_weight=0.0`
- `max_weight=0.20`
- `cash_reserve=None`
- `frontier_points=40`
- `risk_free_rate=None`
- `selected_tickers=None`

Returns:

- `summary`
- `allocation`
- `strategy_metrics`
- `efficient_frontier`
- `efficient_frontier_weights`
- `expected_returns`
- `covariance_matrix`

Important behavior:

- Selected eligible securities are optimized.
- Open securities that are not selected remain fixed.
- Selected securities with insufficient market history may also remain fixed.
- Cash remains a reserve and is not treated like a risky asset.
- The backend validates whether min/max constraints are feasible.
- Efficient-frontier weights are already provided. Do not solve them again in the UI.

Frontend controls must validate obvious ranges before calling the backend:

- `lookback_years > 0`
- `minimum_observations > 1`
- `0 <= min_weight <= max_weight <= 1`
- `frontier_points >= 2`
- Optional cash reserve must be within the backend-accepted range.

Display at minimum:

- Current sleeve
- Equal weight
- Minimum variance
- Maximum Sharpe
- Efficient-frontier risk/return chart
- Target allocation table
- Selected/fixed/insufficient-history status
- Risk-free rate used
- Analysis period and common observation count

### Rebalancing

`get_rebalancing_dashboard_data(...)` arguments:

- `strategy="maximum_sharpe"`
- `frontier_point=None`
- `commission_per_trade=0.0`
- `minimum_trade_amount=5.0`
- `fractional_shares=True`
- All optimization parameters listed above

Allowed strategies:

- `equal_weight`
- `minimum_variance`
- `maximum_sharpe`
- `frontier`

When strategy is `frontier`, require a valid frontier point selected from returned frontier data.

Returns:

- `summary`
- `recommendations`
- `optimization`, containing summary, allocation, metrics, frontier and frontier weights

Display recommendation actions exactly as returned, including `BUY`, `SELL`, `HOLD` and cash-reserve states. Make clear that these are hypothetical recommendations, not orders.

## 13. Page-by-page product requirements

### A. Onboarding and data import

When no valid database exists:

- Explain that the user may choose either a compatible Excel workbook or multiple Midas statement PDFs.
- Offer mutually exclusive `Excel` and `Midas` source choices.
- For Excel, show the required sheets and columns and offer one `.xlsx` uploader.
- For Midas, offer a multi-PDF uploader, SEC contact-email field, analysis progress, warnings and explicit corporate-action review.
- Require overwrite confirmation if source data already exists.
- Never finalize a Midas import while any action decision is pending or any issue is `REVIEW_REQUIRED`.
- Run Midas preview/finalization progress first when applicable, then run the common eight-stage pipeline with live progress.
- Show final elapsed time and validation summary.
- Route to Overview only after validation succeeds.

### B. Portfolio Overview

Top metrics:

- Portfolio value
- Securities value
- Cash balance and cash weight
- Daily return
- Cumulative portfolio TWR
- Total P&L

Charts:

- Portfolio closing value over time
- Cumulative portfolio TWR over time
- Allocation by ticker
- Allocation by sector or asset type

Tables:

- Open holdings
- Recent activity

Also show valuation date and a compact system-health badge.

### C. Transactions

Use tabs:

1. Trades
2. Cash movements
3. Dividends
4. Portfolio import

The fourth tab must support both Excel replacement and Midas statement replacement. Do not put the Midas review inside a transaction edit form.

For each source type:

- List current rows.
- Add form.
- Edit by stable ID.
- Delete with confirmation.
- Show progress during refresh.
- Reload table after success.
- Preserve full numeric precision in form values.

Derived trade fields must be visible as read-only columns, not editable form inputs.

### D. Security Analysis

- Ticker selector populated from `get_available_security_tickers()`.
- Open/closed status and metadata.
- Date range filter.
- Price chart.
- Position value chart.
- Cumulative security TWR chart.
- Daily return chart or table.
- Trade-flow and dividend markers when useful.
- Performance table from `get_security_performance()`.

Closed and delisted positions must remain selectable when daily history exists.

### E. Risk

- Date range inputs.
- Optional manual risk-free-rate override.
- Summary metrics.
- Drawdown visualization using backend-returned history where available.
- Security risk metrics table.
- Risk-contribution chart.
- Correlation heatmap.
- Clear explanation of VaR/CVaR as historical estimates, not guarantees.

### F. Optimization

- Multi-select open tickers.
- Lookback years.
- Minimum observations.
- Minimum and maximum weights.
- Optional cash reserve.
- Number of frontier points.
- Optional risk-free-rate override.
- Run button; do not run expensive optimization on every widget rerun.
- Strategy comparison table.
- Efficient-frontier chart.
- Target allocation table.
- Frontier-point inspection using stored frontier weights.

### G. Rebalancing

- Reuse optimization controls.
- Strategy selector.
- Frontier point when relevant.
- Commission per trade.
- Minimum trade amount.
- Fractional-shares toggle.
- Recommendations table.
- Summary of buys, sells and resulting cash reserve.
- Mandatory non-advice disclaimer.

### H. System Health

- Call `get_portfolio_health()`.
- Show all 11 checks and their messages.
- Provide a manual refresh action.
- Provide test/run instructions, but do not run destructive commands from the UI.
- Show application/backend version information if available.

## 14. Streamlit state and caching rules

Use `st.session_state` for:

- Selected page
- Form mode and selected record ID
- User-selected date ranges
- Optimization parameters
- Last successful analysis result
- Confirmation state
- Selected import source
- Midas preview object
- Midas corporate-action decisions keyed by action ID
- Midas import stage/status while the user reviews candidates

Caching rules:

- Cache only read-only service calls whose inputs fully define the result.
- Never cache mutations, pipeline execution, refresh execution or validation that must reflect a new write.
- After any successful upload or mutation, clear all relevant `st.cache_data` entries.
- Avoid `st.cache_resource` for SQLite connections; backend helpers open and close connections safely.
- Expensive risk/optimization/rebalancing results should be stored in session state after an explicit Run action, not recomputed on every rerun.

## 15. Formatting rules

Formatting belongs in reusable frontend helpers and must never alter source values.

Recommended display:

- USD values: currency with two decimals.
- Weights and returns: percentage with two decimals.
- Shares: enough decimals to preserve fractional shares.
- Prices: generally two to four decimals depending on magnitude.
- Dates: consistent ISO or localized display, while service inputs remain parseable dates.
- Missing values: em dash or a clear `N/A`.

Color semantics:

- Positive P&L/return: positive color.
- Negative P&L/return: negative color.
- Do not use color as the only signal; include signs and labels.
- Validation PASS/WARN/FAIL must have text plus color/icon.

Never transform `0.887` TWR into `0.887%`. A return stored as a decimal must be displayed as `88.70%`.

## 16. Error handling and user messages

Catch expected backend exceptions around button-triggered operations:

- `ValueError`: invalid user input or unsupported selection.
- `RuntimeError`: refresh, download, reconciliation, validation or rollback failure.
- Other exceptions: unexpected technical failure.

Rules:

1. Never silently swallow an exception.
2. Show a short plain-language error first.
3. Put the original exception text in an expandable technical-details section.
4. Do not claim that data was saved until the full refresh returns successfully.
5. If Yahoo Finance fails, explain that existing data may have been preserved or restored according to the backend operation.
6. Keep the previous screen state where possible.
7. Do not expose filesystem secrets, environment variables or credentials.

## 17. Security and privacy

- Never upload `portfolio.db` or user workbooks to Git.
- Do not print full portfolio contents to application logs by default.
- Do not introduce analytics or external telemetry without approval.
- Do not add secrets directly to source files.
- If future configuration is required, use environment variables and an ignored `.env` file.
- Uploaded Excel files must be temporary and deleted after processing.
- Uploaded Midas PDFs and generated temporary workbooks must be deleted after processing and must never be written using user-supplied paths.
- Do not persist the SEC contact email in SQLite, generated workbooks or application logs.
- Do not let user-supplied file names determine arbitrary filesystem paths.

## 18. Testing requirements

Before frontend work:

```bash
python -m pip install -r requirements.txt
python -m pytest -v
```

Expected backend baseline: `70 passed`.

After each frontend milestone:

1. Run all existing tests.
2. Add focused frontend tests where logic can be tested without launching a browser.
3. Run a Streamlit smoke test.
4. Manually test one valid path and one error path.

Minimum new automated tests:

- Frontend bootstrap imports backend services.
- Display formatting does not mutate numeric values.
- Progress adapter handles started/completed/failed events.
- Page data adapters tolerate empty DataFrames.
- Mutation handler clears caches only after success.
- Date and parameter validation rejects invalid ranges.
- Midas uploaded bytes retain safe display names without controlling filesystem paths.
- `REVIEW_REQUIRED` blocks Midas finalization.
- Every Midas action requires an explicit approve/reject decision.
- A finalized Midas workbook is passed to the same common portfolio pipeline used by Excel uploads.

Do not weaken or delete backend tests to make frontend tests pass.

## 19. Acceptance criteria

Frontend v1 is complete only when all conditions are true:

1. A fresh clone can install dependencies using documented commands.
2. If no database exists, the app shows onboarding instead of crashing.
3. A valid workbook can run through all eight stages with visible progress.
4. Multiple valid Midas statements can be analysed, reviewed, finalized into a compatible workbook and passed through all eight common stages.
5. A Midas import cannot continue with unresolved evidence or pending decisions.
6. A failed workbook or Midas import shows the backend error and no false success.
7. Overview uses dashboard-service results without duplicate financial math.
8. Open and closed holdings display correctly.
9. Trade, cash and dividend CRUD use transaction-service functions.
10. Old trades and split-adjusted histories remain backend-managed.
11. Failed CRUD refresh restores the database and the UI reports failure.
12. Security analysis includes open and closed/delisted tickers when history exists.
13. Risk page shows the risk-free-rate source, observation date and rate.
14. Optimization accepts selected tickers and min/max constraints.
15. Frontier points display stored weights as well as risk/return.
16. Rebalancing supports all backend strategies and never sends orders.
17. Health page displays all validation checks.
18. All backend and frontend tests pass.
19. Personal databases, workbooks and statements remain ignored by Git.
20. README contains frontend setup and run instructions.

## 20. Required implementation sequence

Work in small reviewable milestones. Do not generate the entire frontend in one unverified step.

### Milestone 1 - Foundation

- Add and pin Streamlit.
- Create frontend directory and bootstrap.
- Create app shell and navigation.
- Detect database availability.
- Add a minimal smoke test.
- Run all tests.

### Milestone 2 - Import and progress

- Add the Excel/Midas source selector.
- Add Excel upload and multi-PDF Midas upload.
- Implement safe temporary-file handling for Excel, PDFs and generated workbook bytes.
- Implement separate Midas and common-pipeline progress callback adapters.
- Add replacement confirmation.
- Add Midas SEC email, preview, warnings, action review and explicit decision controls.
- Display validation outcome.
- Test Excel success/failure and Midas preview/finalize/blocking paths.

### Milestone 3 - Overview

- Use `get_overview_dashboard_data()`.
- Add metrics, portfolio history, allocations, holdings and activity.
- Add empty-state and health-state handling.

### Milestone 4 - Transactions

- Build trade CRUD first.
- Then cash movement CRUD.
- Then dividend CRUD.
- Add delete confirmation and refresh progress.
- Verify rollback errors are displayed.

### Milestone 5 - Security analysis

- Ticker selector.
- History and TWR charts.
- Security performance table.
- Closed/delisted position behavior.

### Milestone 6 - Risk

- Controls, explicit Run action, metrics, matrices and charts.
- Show risk-free-rate details.

### Milestone 7 - Optimization

- Ticker and constraint controls.
- Strategy metrics and allocation.
- Efficient frontier and stored frontier weights.

### Milestone 8 - Rebalancing

- Strategy selection and assumptions.
- Recommendations and disclaimer.

### Milestone 9 - Health, polish and documentation

- Health page.
- Consistent formatting and errors.
- Accessibility and responsive layout.
- README update.
- Full tests and manual acceptance pass.

At the end of every milestone, report:

- Files created or changed
- Backend functions used
- Tests run and results
- Remaining risks
- Exact next milestone

## 21. Change-management rules

- Work on a dedicated frontend branch, for example `frontend/streamlit-v1`.
- Keep commits small and descriptive.
- Never rewrite or force-push `backend-v1.0`.
- Before every merge, run the full test suite.
- If backend changes are approved, add regression tests and document the changed contract here.
- Do not commit generated databases, uploaded workbooks, virtual environments, caches or secrets.

Suggested first commands:

```bash
git checkout main
git pull origin main
git checkout -b frontend/streamlit-v1
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -v
```

## 22. Ready-to-use AI implementation prompt

Copy the following prompt into the AI coding tool used by the frontend team:

```text
You are the implementation engineer for the Streamlit frontend of this repository.

Your objective is to build a reliable local single-user frontend on top of the completed backend without duplicating or changing financial calculations.

Authoritative context:
1. Read FRONTEND_HANDOFF.md completely.
2. Read README.md.
3. Inspect the current backend source files and tests referenced by the handoff.
4. Run the full test suite before editing anything.
5. Treat current backend code and tests as the source of truth if documentation has drifted.

Hard constraints:
- Do not calculate TWR, P&L, holdings, cash, risk, optimization or rebalancing inside Streamlit.
- Do not write directly to SQLite.
- Use dashboard_service for reads.
- Use transaction_service for all CRUD writes.
- Use run_portfolio_pipeline for the common workbook replacement import.
- Use prepare_midas_portfolio_import and finalize_midas_portfolio_import for Midas uploads; never call the terminal CLI from Streamlit.
- Keep refresh_calculated_data=True for every user mutation.
- Do not bypass snapshot rollback, validation, split handling or delisted-ticker handling.
- Do not hard-code TWTR or any ticker-specific fallback.
- Never auto-approve Midas corporate actions or bypass REVIEW_REQUIRED.
- Preserve numeric precision; round only at display time.
- Never commit portfolio.xlsx, portfolio.db, statements, secrets, caches or virtual environments.
- Do not implement public multi-user hosting, Monte Carlo, taxes, FX conversion or brokerage execution.
- Do not change backend behavior without first presenting evidence, adding a failing regression test and receiving explicit approval.

Work process:
- Implement only one milestone from FRONTEND_HANDOFF.md at a time.
- Before each milestone, state the files you will inspect and change.
- Prefer small reusable frontend modules.
- Add tests for frontend adapters and state logic.
- Run the entire test suite after each milestone.
- Do not continue if existing backend tests fail.
- Explain errors in plain language to the human collaborators.
- Never claim success without reporting the exact tests run and their results.

Start with Milestone 1 only:
1. Verify repository status and backend test baseline.
2. Create a dedicated frontend branch.
3. Add and pin a Streamlit version compatible with the active Python environment.
4. Create the frontend bootstrap, application shell, navigation and database-empty state.
5. Add a minimal frontend smoke test.
6. Run all tests.
7. Stop and report files changed, backend functions used, results, risks and the proposed Milestone 2 plan.

Do not implement Milestone 2 until the human explicitly approves it.
```

## 23. Assumptions and unresolved decisions

Verified assumptions:

- The current backend baseline is 70 passing tests and 11 passing portfolio-validation checks.
- The application is currently local and single-user.
- USD is the only portfolio currency.
- Streamlit is the selected frontend framework.
- Plotly is already available for interactive charts.

Decisions that the frontend team may make without backend changes:

- Visual theme
- Page layout
- Chart colors
- Navigation style
- Table pagination
- Display precision within the rules above
- Turkish or English UI labels

Decisions requiring owner approval:

- Any backend API change
- Any database schema change
- Public deployment or multi-user support
- Authentication
- Cloud storage
- New asset classes or currencies
- New financial models

## 24. Completion handoff

When frontend v1 is complete, provide:

- Updated repository and commit history
- Frontend setup/run documentation
- Full automated test output
- Manual acceptance checklist
- Screenshots of all pages
- Known limitations
- Confirmation that ignored personal files were not committed
- A new release tag proposed for owner approval

Do not overwrite the stable `backend-v1.0` tag.
