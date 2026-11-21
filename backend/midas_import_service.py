from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from midas_pdf_parser import (
    CASH_MOVEMENT_COLUMNS,
    DIVIDEND_COLUMNS,
    HOLDING_COLUMNS,
    TRADE_COLUMNS,
    UNSUPPORTED_ACTIVITY_COLUMNS,
    ParsedMidasStatement,
    PdfSource,
    parse_midas_statement,
)


@dataclass(frozen=True)
class MidasStatementInput:
    """
    One PDF supplied as a path, bytes, or uploaded file.

    source_name is required only when raw bytes do not carry
    a filename.
    """

    source: PdfSource
    source_name: str | None = None


@dataclass(frozen=True)
class MidasLoadProgress:
    """
    Frontend-friendly PDF loading progress.
    """

    file_index: int
    total_files: int
    source_name: str
    status: str
    message: str
    progress: float


MidasProgressCallback = Callable[
    [MidasLoadProgress],
    None,
]


@dataclass
class CombinedMidasStatements:
    """
    Combined raw records from multiple Midas statements.
    """

    trades: pd.DataFrame
    cash_movements: pd.DataFrame
    dividends: pd.DataFrame
    holdings: pd.DataFrame
    unsupported_activities: pd.DataFrame
    statement_summaries: list[dict[str, Any]]
    duplicate_pdf_names: list[str]
    duplicates_removed: dict[str, int]
    ignored_order_count: int
    warnings: list[str]

    def summary(self) -> dict[str, Any]:
        """
        Return a frontend-friendly combined summary.
        """

        return {
            "status": "READY_FOR_VALIDATION",
            "statement_count": len(
                self.statement_summaries
            ),
            "duplicate_pdf_count": len(
                self.duplicate_pdf_names
            ),
            "trade_count": len(self.trades),
            "cash_movement_count": len(
                self.cash_movements
            ),
            "dividend_count": len(
                self.dividends
            ),
            "holding_snapshot_count": len(
                self.holdings
            ),
            "unsupported_activity_count": len(
                self.unsupported_activities
            ),
            "ignored_order_count": (
                self.ignored_order_count
            ),
            "duplicates_removed": (
                self.duplicates_removed.copy()
            ),
            "duplicate_pdf_names": (
                self.duplicate_pdf_names.copy()
            ),
            "warnings": self.warnings.copy(),
        }


def console_progress_callback(
    event: MidasLoadProgress,
) -> None:
    """
    Display progress while testing from the terminal.
    """

    prefix = (
        f"[{event.file_index}/{event.total_files}]"
    )

    print(
        f"{prefix} {event.source_name}: "
        f"{event.message}"
    )


def source_display_name(
    item: MidasStatementInput,
    index: int,
) -> str:
    """
    Determine a safe display name before parsing.
    """

    if item.source_name:
        return Path(item.source_name).name

    if isinstance(item.source, (str, Path)):
        return Path(item.source).name

    uploaded_name = getattr(
        item.source,
        "name",
        None,
    )

    if uploaded_name:
        return Path(str(uploaded_name)).name

    return f"uploaded_statement_{index}.pdf"


def emit_progress(
    callback: MidasProgressCallback | None,
    file_index: int,
    total_files: int,
    source_name: str,
    status: str,
    message: str,
) -> None:
    """
    Send a loading event to the terminal or frontend.
    """

    if callback is None:
        return

    callback(
        MidasLoadProgress(
            file_index=file_index,
            total_files=total_files,
            source_name=source_name,
            status=status,
            message=message,
            progress=(
                file_index / total_files
                if total_files
                else 1.0
            ),
        )
    )


def normalize_input(
    item: PdfSource | MidasStatementInput,
) -> MidasStatementInput:
    """
    Normalize public input values.
    """

    if isinstance(item, MidasStatementInput):
        return item

    return MidasStatementInput(source=item)


def normalized_key_value(
    value: Any,
) -> Any:
    """
    Normalize values used in transaction fingerprints.
    """

    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, float):
        return round(value, 10)

    if isinstance(value, str):
        return value.strip()

    return value


def build_event_key(
    row: pd.Series,
    key_columns: list[str],
) -> tuple[Any, ...]:
    """
    Build a stable financial-event fingerprint.
    """

    return tuple(
        normalized_key_value(row[column])
        for column in key_columns
    )


def source_contains_event_date(
    group: pd.DataFrame,
    source_hash: str,
) -> bool:
    """
    Prefer the statement whose reporting period contains
    the event date.

    This selects an April statement over a May statement
    when an April transaction appears in both files.
    """

    source_rows = group.loc[
        group["source_hash"] == source_hash
    ]

    if source_rows.empty:
        return False

    row = source_rows.iloc[0]

    event_date = row.get("date")

    if event_date is None:
        return False

    try:
        return bool(
            row["statement_start"]
            <= event_date
            <= row["statement_end"]
        )

    except (TypeError, KeyError):
        return False


def choose_preferred_source(
    group: pd.DataFrame,
) -> tuple[str, int]:
    """
    Select the source that contains the largest legitimate
    occurrence count for one event.

    If the same transaction occurs twice inside one PDF,
    both occurrences are retained. Repetitions across other
    PDFs are removed.
    """

    counts = (
        group.groupby(
            "source_hash",
            sort=False,
        )
        .size()
    )

    maximum_occurrences = int(counts.max())

    candidate_hashes = [
        str(source_hash)
        for source_hash, count in counts.items()
        if int(count) == maximum_occurrences
    ]

    in_period_candidates = [
        source_hash
        for source_hash in candidate_hashes
        if source_contains_event_date(
            group,
            source_hash,
        )
    ]

    if in_period_candidates:
        candidate_hashes = in_period_candidates

    source_order = (
        group.groupby(
            "source_hash",
            sort=False,
        )["_source_order"]
        .min()
        .to_dict()
    )

    selected_hash = min(
        candidate_hashes,
        key=lambda source_hash: source_order[
            source_hash
        ],
    )

    return selected_hash, maximum_occurrences


def combine_and_deduplicate(
    frames: list[pd.DataFrame],
    output_columns: list[str],
    key_columns: list[str],
    sort_columns: list[str],
) -> tuple[pd.DataFrame, int]:
    """
    Combine event tables and safely remove cross-PDF
    repetitions.
    """

    nonempty_frames = [
        frame.copy()
        for frame in frames
        if not frame.empty
    ]

    if not nonempty_frames:
        return (
            pd.DataFrame(columns=output_columns),
            0,
        )

    combined = pd.concat(
        nonempty_frames,
        ignore_index=True,
    )

    combined["_row_order"] = range(len(combined))

    combined["_event_key"] = combined.apply(
        lambda row: build_event_key(
            row,
            key_columns,
        ),
        axis=1,
    )

    selected_indices: list[int] = []

    for _, group in combined.groupby(
        "_event_key",
        sort=False,
    ):
        (
            selected_source_hash,
            maximum_occurrences,
        ) = choose_preferred_source(group)

        selected_rows = (
            group.loc[
                group["source_hash"]
                == selected_source_hash
            ]
            .sort_values(
                ["_source_order", "_row_order"],
                kind="stable",
            )
            .head(maximum_occurrences)
        )

        selected_indices.extend(
            selected_rows.index.tolist()
        )

    result = (
        combined.loc[selected_indices]
        .sort_values(
            sort_columns
            + ["_source_order", "_row_order"],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    removed_count = len(combined) - len(result)

    return (
        result[output_columns].copy(),
        removed_count,
    )


def combine_holdings(
    frames: list[pd.DataFrame],
) -> tuple[pd.DataFrame, int]:
    """
    Combine monthly holding snapshots.

    Conflicting snapshots for the same ticker and date are
    rejected instead of selecting one silently.
    """

    nonempty_frames = [
        frame.copy()
        for frame in frames
        if not frame.empty
    ]

    if not nonempty_frames:
        return (
            pd.DataFrame(columns=HOLDING_COLUMNS),
            0,
        )

    combined = pd.concat(
        nonempty_frames,
        ignore_index=True,
    )

    identity_columns = [
        "statement_date",
        "ticker",
    ]

    value_columns = [
        "company_name",
        "shares",
        "average_cost_usd",
        "profit_loss_usd",
        "market_value_usd",
    ]

    selected_indices: list[int] = []
    removed_count = 0

    for identity, group in combined.groupby(
        identity_columns,
        sort=False,
        dropna=False,
    ):
        value_keys = {
            tuple(
                normalized_key_value(row[column])
                for column in value_columns
            )
            for _, row in group.iterrows()
        }

        if len(value_keys) > 1:
            statement_date, ticker = identity

            source_files = sorted(
                group["source_file"]
                .astype(str)
                .unique()
                .tolist()
            )

            raise ValueError(
                "Conflicting holding snapshots found "
                f"for {ticker} on {statement_date}. "
                "Source files: "
                + ", ".join(source_files)
            )

        preferred_row = (
            group.sort_values(
                ["_source_order"],
                kind="stable",
            )
            .iloc[0]
        )

        selected_indices.append(
            int(preferred_row.name)
        )

        removed_count += len(group) - 1

    result = (
        combined.loc[selected_indices]
        .sort_values(
            [
                "statement_date",
                "ticker",
                "_source_order",
            ],
            kind="stable",
        )
        .reset_index(drop=True)
    )

    return (
        result[HOLDING_COLUMNS].copy(),
        removed_count,
    )


def build_period_warnings(
    statements: list[ParsedMidasStatement],
) -> list[str]:
    """
    Detect missing, overlapping, or repeated statement
    periods without blocking the import yet.
    """

    warnings: list[str] = []

    unique_periods = sorted({
        (
            statement.statement_start,
            statement.statement_end,
        )
        for statement in statements
    })

    period_counts: dict[
        tuple[Any, Any],
        int,
    ] = {}

    for statement in statements:
        period = (
            statement.statement_start,
            statement.statement_end,
        )

        period_counts[period] = (
            period_counts.get(period, 0) + 1
        )

    for period, count in period_counts.items():
        if count > 1:
            start_date, end_date = period

            warnings.append(
                "Multiple PDF files cover the same "
                f"statement period: {start_date} to "
                f"{end_date}."
            )

    for previous, current in zip(
        unique_periods,
        unique_periods[1:],
    ):
        previous_start, previous_end = previous
        current_start, current_end = current

        expected_start = (
            previous_end + timedelta(days=1)
        )

        if current_start > expected_start:
            missing_end = (
                current_start - timedelta(days=1)
            )

            warnings.append(
                "A statement-period gap was detected: "
                f"{expected_start} to {missing_end}."
            )

        elif current_start <= previous_end:
            warnings.append(
                "Overlapping statement periods were "
                f"detected: {previous_start} to "
                f"{previous_end}, and {current_start} "
                f"to {current_end}."
            )

    return warnings


def load_midas_statements(
    sources: Sequence[
        PdfSource | MidasStatementInput
    ],
    progress_callback: (
        MidasProgressCallback | None
    ) = None,
) -> CombinedMidasStatements:
    """
    Parse and combine multiple Midas PDFs.

    The function performs no ticker conversion, corporate
    action inference, Excel write, or database mutation.
    """

    normalized_sources = [
        normalize_input(item)
        for item in sources
    ]

    if not normalized_sources:
        raise ValueError(
            "At least one Midas PDF is required."
        )

    total_files = len(normalized_sources)

    parsed_statements: list[
        ParsedMidasStatement
    ] = []

    duplicate_pdf_names: list[str] = []
    seen_hashes: set[str] = set()

    for index, item in enumerate(
        normalized_sources,
        start=1,
    ):
        display_name = source_display_name(
            item,
            index,
        )

        emit_progress(
            callback=progress_callback,
            file_index=index,
            total_files=total_files,
            source_name=display_name,
            status="started",
            message="Reading statement...",
        )

        try:
            statement = parse_midas_statement(
                source=item.source,
                source_name=item.source_name,
            )

        except Exception as error:
            emit_progress(
                callback=progress_callback,
                file_index=index,
                total_files=total_files,
                source_name=display_name,
                status="failed",
                message=str(error),
            )

            raise ValueError(
                f"{display_name} could not be "
                f"processed: {error}"
            ) from error

        if statement.source_hash in seen_hashes:
            duplicate_pdf_names.append(
                statement.source_file
            )

            emit_progress(
                callback=progress_callback,
                file_index=index,
                total_files=total_files,
                source_name=display_name,
                status="skipped",
                message=(
                    "Identical PDF content skipped."
                ),
            )

            continue

        seen_hashes.add(statement.source_hash)
        parsed_statements.append(statement)

        emit_progress(
            callback=progress_callback,
            file_index=index,
            total_files=total_files,
            source_name=display_name,
            status="completed",
            message=(
                f"{len(statement.trades)} trades, "
                f"{len(statement.cash_movements)} "
                "cash movements, "
                f"{len(statement.dividends)} "
                "dividends."
            ),
        )

    if not parsed_statements:
        raise ValueError(
            "No unique Midas statement could be read."
        )

    trade_frames: list[pd.DataFrame] = []
    cash_frames: list[pd.DataFrame] = []
    dividend_frames: list[pd.DataFrame] = []
    holding_frames: list[pd.DataFrame] = []
    unsupported_frames: list[pd.DataFrame] = []

    for source_order, statement in enumerate(
        parsed_statements
    ):
        for frame, target in [
            (statement.trades, trade_frames),
            (
                statement.cash_movements,
                cash_frames,
            ),
            (statement.dividends, dividend_frames),
            (statement.holdings, holding_frames),
            (
                statement.unsupported_activities,
                unsupported_frames,
            ),
        ]:
            prepared = frame.copy()
            prepared["_source_order"] = source_order
            target.append(prepared)

    trades, removed_trades = (
        combine_and_deduplicate(
            frames=trade_frames,
            output_columns=TRADE_COLUMNS,
            key_columns=[
                "date",
                "time",
                "ticker",
                "side",
                "shares",
                "price_usd",
                "commission_usd",
                "is_gift",
            ],
            sort_columns=[
                "date",
                "time",
            ],
        )
    )

    cash_movements, removed_cash = (
        combine_and_deduplicate(
            frames=cash_frames,
            output_columns=CASH_MOVEMENT_COLUMNS,
            key_columns=[
                "date",
                "time",
                "type",
                "amount_usd",
            ],
            sort_columns=[
                "date",
                "time",
            ],
        )
    )

    dividends, removed_dividends = (
        combine_and_deduplicate(
            frames=dividend_frames,
            output_columns=DIVIDEND_COLUMNS,
            key_columns=[
                "date",
                "ticker",
                "net_dividend_usd",
            ],
            sort_columns=[
                "date",
                "ticker",
            ],
        )
    )

    holdings, removed_holdings = (
        combine_holdings(holding_frames)
    )

    nonempty_unsupported = [
        frame
        for frame in unsupported_frames
        if not frame.empty
    ]

    if nonempty_unsupported:
        unsupported_activities = pd.concat(
            nonempty_unsupported,
            ignore_index=True,
        )[UNSUPPORTED_ACTIVITY_COLUMNS].copy()

    else:
        unsupported_activities = pd.DataFrame(
            columns=UNSUPPORTED_ACTIVITY_COLUMNS
        )

    warnings = build_period_warnings(
        parsed_statements
    )

    for statement in parsed_statements:
        warnings.extend(statement.warnings)

    if duplicate_pdf_names:
        warnings.append(
            f"{len(duplicate_pdf_names)} identical "
            "PDF upload was skipped."
        )

    duplicates_removed = {
        "trades": removed_trades,
        "cash_movements": removed_cash,
        "dividends": removed_dividends,
        "holdings": removed_holdings,
    }

    if sum(duplicates_removed.values()) > 0:
        warnings.append(
            "Repeated records found across statements "
            "were removed safely."
        )

    return CombinedMidasStatements(
        trades=trades,
        cash_movements=cash_movements,
        dividends=dividends,
        holdings=holdings,
        unsupported_activities=(
            unsupported_activities
        ),
        statement_summaries=[
            statement.summary()
            for statement in parsed_statements
        ],
        duplicate_pdf_names=duplicate_pdf_names,
        duplicates_removed=duplicates_removed,
        ignored_order_count=sum(
            statement.ignored_order_count
            for statement in parsed_statements
        ),
        warnings=list(dict.fromkeys(warnings)),
    )


def collect_pdf_paths(
    folder: Path,
) -> list[Path]:
    """
    Find every PDF directly inside a folder.
    """

    if not folder.exists():
        raise FileNotFoundError(
            f"Folder does not exist: {folder}"
        )

    if not folder.is_dir():
        raise ValueError(
            f"Path is not a folder: {folder}"
        )

    pdf_paths = sorted(
        (
            path
            for path in folder.iterdir()
            if (
                path.is_file()
                and path.suffix.lower() == ".pdf"
            )
        ),
        key=lambda path: path.name.casefold(),
    )

    if not pdf_paths:
        raise FileNotFoundError(
            f"No PDF files found in: {folder}"
        )

    return pdf_paths


def build_argument_parser() -> ArgumentParser:
    """
    Build command-line arguments.
    """

    parser = ArgumentParser(
        description=(
            "Read and combine multiple Midas statements "
            "without modifying the portfolio database."
        )
    )

    parser.add_argument(
        "pdf_paths",
        nargs="*",
        type=Path,
        help="One or more Midas PDF paths.",
    )

    parser.add_argument(
        "--folder",
        type=Path,
        help=(
            "Read every PDF directly inside this folder."
        ),
    )

    return parser


def main() -> None:
    """
    Test multiple-PDF loading from the terminal.
    """

    arguments = build_argument_parser().parse_args()

    if arguments.folder is not None:
        if arguments.pdf_paths:
            raise ValueError(
                "Use either --folder or individual "
                "PDF paths, not both."
            )

        pdf_paths = collect_pdf_paths(
            arguments.folder
        )

    else:
        pdf_paths = arguments.pdf_paths

    result = load_midas_statements(
        sources=pdf_paths,
        progress_callback=(
            console_progress_callback
        ),
    )

    print()
    print("Midas statements combined successfully.")
    print()

    for key, value in result.summary().items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
