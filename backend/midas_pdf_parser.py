from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO
import re

import pandas as pd
import pdfplumber


PdfSource = str | Path | bytes | bytearray | BinaryIO

NUMBER_PATTERN = r"-?[\d.,]+"
TICKER_PATTERN = r"[A-Z0-9.^-]+"


STATEMENT_PERIOD_RE = re.compile(
    r"(?P<start>\d{2}/\d{2}/\d{2})\s*-\s*"
    r"(?P<end>\d{2}/\d{2}/\d{2})\s+"
    r"HESAP EKSTRESİ"
)

TRADE_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r".+?\s+Emri\s+"
    rf"(?P<ticker>{TICKER_PATTERN})\s+"
    r"(?P<side>Alış|Satış)\s+"
    r"(?P<status>"
    r"Gerçekleşti|İptal Edildi|Süresi Doldu|Reddedildi"
    r")\s+"
    r"(?P<currency>[A-Z]{3})\s+"
    r"(?P<order_shares>\S+)\s+"
    r"(?P<order_amount>\S+)\s+"
    r"(?P<shares>\S+)\s+"
    r"(?P<price>\S+)\s+"
    r"(?P<commission>\S+)\s+"
    r"(?P<gross>\S+)$"
)

GIFT_TRADE_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"Hediye Hisse\s+"
    rf"(?P<ticker>{TICKER_PATTERN})\s+"
    r"Hediye Hisse\s+"
    r"Gerçekleşti\s+USD\s+"
    r"\S+\s+\S+\s+"
    r"(?P<shares>\S+)\s+"
    r"(?P<price>\S+)\s+"
    r"(?P<commission>\S+)\s+"
    r"(?P<gross>\S+)$"
)

CASH_MOVEMENT_RE = re.compile(
    r"^(?P<request_date>\d{2}/\d{2}/\d{2})\s+"
    r"(?P<request_time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<date>\d{2}/\d{2}/\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<status>Gerçekleşti|İptal Edildi)\s+"
    rf"(?P<amount>{NUMBER_PATTERN})\s+USD$"
)

DIVIDEND_RE = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{2})\s+"
    rf"(?P<ticker>{TICKER_PATTERN})\s+-\s+"
    r"(?P<company_name>.+?)\s+"
    rf"(?P<gross>{NUMBER_PATTERN})\s+USD\s+"
    rf"(?:(?P<withholding>{NUMBER_PATTERN})\s+USD|-)\s+"
    rf"(?P<net>{NUMBER_PATTERN})\s+USD$"
)

HOLDING_RE = re.compile(
    rf"^(?P<ticker>{TICKER_PATTERN})\s+-\s+"
    r"(?P<company_name>.+?)\s+"
    rf"(?P<shares>{NUMBER_PATTERN})\s+"
    rf"(?P<average_cost>{NUMBER_PATTERN})\s+USD\s+"
    rf"(?P<profit_loss>{NUMBER_PATTERN})\s+USD\s+"
    rf"(?P<market_value>{NUMBER_PATTERN})\s+USD$"
)


TRADE_COLUMNS = [
    "date",
    "time",
    "ticker",
    "side",
    "shares",
    "price_usd",
    "commission_usd",
    "gross_amount_usd",
    "is_gift",
    "source_file",
    "source_hash",
    "statement_start",
    "statement_end",
    "source_page",
]

CASH_MOVEMENT_COLUMNS = [
    "date",
    "time",
    "type",
    "amount_usd",
    "description",
    "request_date",
    "request_time",
    "source_file",
    "source_hash",
    "statement_start",
    "statement_end",
    "source_page",
]

DIVIDEND_COLUMNS = [
    "date",
    "time",
    "ticker",
    "company_name",
    "gross_dividend_usd",
    "withholding_tax_usd",
    "net_dividend_usd",
    "source_file",
    "source_hash",
    "statement_start",
    "statement_end",
    "source_page",
]

HOLDING_COLUMNS = [
    "statement_date",
    "ticker",
    "company_name",
    "shares",
    "average_cost_usd",
    "profit_loss_usd",
    "market_value_usd",
    "source_file",
    "source_hash",
    "statement_start",
    "statement_end",
    "source_page",
]

UNSUPPORTED_ACTIVITY_COLUMNS = [
    "section",
    "raw_line",
    "source_file",
    "source_hash",
    "statement_start",
    "statement_end",
    "source_page",
]


class MidasPdfParseError(ValueError):
    """
    Raised when a Midas statement cannot be read safely.
    """


@dataclass
class ParsedMidasStatement:
    source_file: str
    source_hash: str
    statement_start: date
    statement_end: date
    trades: pd.DataFrame
    cash_movements: pd.DataFrame
    dividends: pd.DataFrame
    holdings: pd.DataFrame
    unsupported_activities: pd.DataFrame
    ignored_order_count: int
    warnings: list[str]

    def summary(self) -> dict[str, Any]:
        """
        Return a frontend-friendly statement summary.
        """

        return {
            "source_file": self.source_file,
            "source_hash": self.source_hash,
            "statement_start": (
                self.statement_start.isoformat()
            ),
            "statement_end": (
                self.statement_end.isoformat()
            ),
            "trade_count": len(self.trades),
            "cash_movement_count": (
                len(self.cash_movements)
            ),
            "dividend_count": len(self.dividends),
            "holding_count": len(self.holdings),
            "ignored_order_count": (
                self.ignored_order_count
            ),
            "unsupported_activity_count": (
                len(self.unsupported_activities)
            ),
            "warnings": self.warnings.copy(),
        }


def normalize_pdf_line(value: str) -> str:
    """
    Normalize whitespace without changing financial values.
    """

    return re.sub(
        r"\s+",
        " ",
        value.replace("\u00a0", " "),
    ).strip()


def parse_turkish_number(value: str) -> Decimal:
    """
    Convert Turkish-formatted numbers to Decimal.

    Examples:
        1.234,56 -> Decimal("1234.56")
        -12,50   -> Decimal("-12.50")
        15       -> Decimal("15")
    """

    normalized = value.strip()

    if normalized in {"", "-"}:
        raise MidasPdfParseError(
            f"Numeric value is missing: {value!r}"
        )

    if "," in normalized:
        normalized = (
            normalized
            .replace(".", "")
            .replace(",", ".")
        )

    try:
        return Decimal(normalized)

    except InvalidOperation as error:
        raise MidasPdfParseError(
            f"Invalid numeric value: {value!r}"
        ) from error


def parse_pdf_date(value: str) -> date:
    """
    Convert DD/MM/YY statement dates to date objects.
    """

    try:
        return datetime.strptime(
            value,
            "%d/%m/%y",
        ).date()

    except ValueError as error:
        raise MidasPdfParseError(
            f"Invalid statement date: {value!r}"
        ) from error


def parse_pdf_time(value: str) -> time:
    """
    Convert HH:MM:SS statement times to time objects.
    """

    try:
        return datetime.strptime(
            value,
            "%H:%M:%S",
        ).time()

    except ValueError as error:
        raise MidasPdfParseError(
            f"Invalid statement time: {value!r}"
        ) from error


def read_pdf_source(
    source: PdfSource,
    source_name: str | None = None,
) -> tuple[bytes, str]:
    """
    Read a path, bytes object, or uploaded file-like object.

    Streamlit UploadedFile objects are supported because they
    provide read(), seek(), and usually a name attribute.
    """

    if isinstance(source, (str, Path)):
        path = Path(source)

        if not path.exists():
            raise FileNotFoundError(
                f"PDF file does not exist: {path}"
            )

        if not path.is_file():
            raise ValueError(
                f"PDF source is not a file: {path}"
            )

        content = path.read_bytes()
        resolved_name = source_name or path.name

    elif isinstance(source, (bytes, bytearray)):
        content = bytes(source)
        resolved_name = source_name or "uploaded_statement.pdf"

    elif hasattr(source, "read"):
        original_position: int | None = None

        if hasattr(source, "tell"):
            try:
                original_position = source.tell()
            except Exception:
                original_position = None

        content = source.read()

        if isinstance(content, str):
            raise TypeError(
                "PDF upload returned text instead of bytes."
            )

        content = bytes(content)

        if (
            original_position is not None
            and hasattr(source, "seek")
        ):
            try:
                source.seek(original_position)
            except Exception:
                pass

        uploaded_name = getattr(
            source,
            "name",
            None,
        )

        resolved_name = (
            source_name
            or uploaded_name
            or "uploaded_statement.pdf"
        )

    else:
        raise TypeError(
            "PDF source must be a path, bytes, "
            "or a binary file-like object."
        )

    if not content:
        raise MidasPdfParseError(
            f"PDF file is empty: {resolved_name}"
        )

    if not str(resolved_name).lower().endswith(".pdf"):
        raise MidasPdfParseError(
            f"Only PDF files are supported: {resolved_name}"
        )

    if not content.startswith(b"%PDF"):
        raise MidasPdfParseError(
            f"File does not appear to be a PDF: {resolved_name}"
        )

    return content, Path(str(resolved_name)).name


def find_statement_period(
    page_lines: list[tuple[int, str]],
    source_file: str,
) -> tuple[date, date]:
    """
    Find the reporting period printed in the statement.
    """

    periods: set[tuple[date, date]] = set()

    for _, line in page_lines:
        match = STATEMENT_PERIOD_RE.search(line)

        if not match:
            continue

        start_date = parse_pdf_date(
            match.group("start")
        )

        end_date = parse_pdf_date(
            match.group("end")
        )

        periods.add(
            (start_date, end_date)
        )

    if not periods:
        raise MidasPdfParseError(
            "Statement period could not be found in "
            f"{source_file}."
        )

    if len(periods) > 1:
        period_text = ", ".join(
            f"{start.isoformat()} to {end.isoformat()}"
            for start, end in sorted(periods)
        )

        raise MidasPdfParseError(
            f"Multiple statement periods found in "
            f"{source_file}: {period_text}"
        )

    statement_start, statement_end = periods.pop()

    if statement_start > statement_end:
        raise MidasPdfParseError(
            f"Invalid statement period in {source_file}."
        )

    return statement_start, statement_end


def classify_cash_movement(
    description: str,
) -> str:
    """
    Map Midas cash descriptions to application values.
    """

    normalized = description.casefold()

    if (
        "döviz alış" in normalized
        or "para yatırma" in normalized
    ):
        return "DEPOSIT"

    if (
        "döviz satış" in normalized
        or "para çekme" in normalized
    ):
        return "WITHDRAWAL"

    if "nema geliri" in normalized:
        return "INTEREST"

    raise MidasPdfParseError(
        "Unknown cash movement description: "
        f"{description}"
    )


def parse_midas_statement(
    source: PdfSource,
    source_name: str | None = None,
) -> ParsedMidasStatement:
    """
    Parse one Midas account statement.

    No ticker aliases, stock splits, inferred trades, or
    database writes are applied at this stage.
    """

    content, resolved_name = read_pdf_source(
        source=source,
        source_name=source_name,
    )

    source_hash = sha256(content).hexdigest()

    page_lines: list[tuple[int, str]] = []

    try:
        with pdfplumber.open(
            BytesIO(content)
        ) as pdf:
            if not pdf.pages:
                raise MidasPdfParseError(
                    f"PDF has no pages: {resolved_name}"
                )

            for page_number, page in enumerate(
                pdf.pages,
                start=1,
            ):
                page_text = page.extract_text() or ""

                for raw_line in page_text.splitlines():
                    line = normalize_pdf_line(raw_line)

                    if line:
                        page_lines.append(
                            (page_number, line)
                        )

    except MidasPdfParseError:
        raise

    except Exception as error:
        raise MidasPdfParseError(
            f"PDF could not be opened: {resolved_name}: "
            f"{error}"
        ) from error

    if not page_lines:
        raise MidasPdfParseError(
            f"No readable text was found in {resolved_name}."
        )

    (
        statement_start,
        statement_end,
    ) = find_statement_period(
        page_lines=page_lines,
        source_file=resolved_name,
    )

    trades: list[dict[str, Any]] = []
    cash_movements: list[dict[str, Any]] = []
    dividends: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    unsupported_activities: list[
        dict[str, Any]
    ] = []

    ignored_order_count = 0
    warnings: list[str] = []
    section: str | None = None
    sections_found: set[str] = set()

    shared_source_values = {
        "source_file": resolved_name,
        "source_hash": source_hash,
        "statement_start": statement_start,
        "statement_end": statement_end,
    }

    for page_number, line in page_lines:
        if line.startswith("PORTFÖY ÖZETİ"):
            section = "holdings"
            sections_found.add(section)
            continue

        if line.startswith("YATIRIM İŞLEMLERİ"):
            section = "trades"
            sections_found.add(section)
            continue

        if line.startswith("HESAP İŞLEMLERİ"):
            section = "cash_movements"
            sections_found.add(section)
            continue

        if line.startswith("TEMETTÜ İŞLEMLERİ"):
            section = "dividends"
            sections_found.add(section)
            continue

        if line.startswith("HİSSE TRANSFERLERİ"):
            section = "share_transfers"
            sections_found.add(section)
            continue

        if line.startswith("Kayıt bulunmamaktadır"):
            continue

        if section == "holdings":
            if (
                line.startswith(
                    "Sermaye Piyasası Aracı"
                )
                or line.startswith("*")
            ):
                continue

            match = HOLDING_RE.match(line)

            if match:
                row = match.groupdict()

                holdings.append({
                    "statement_date": statement_end,
                    "ticker": row["ticker"].upper(),
                    "company_name": (
                        row["company_name"].strip()
                    ),
                    "shares": float(
                        parse_turkish_number(
                            row["shares"]
                        )
                    ),
                    "average_cost_usd": float(
                        parse_turkish_number(
                            row["average_cost"]
                        )
                    ),
                    "profit_loss_usd": float(
                        parse_turkish_number(
                            row["profit_loss"]
                        )
                    ),
                    "market_value_usd": float(
                        parse_turkish_number(
                            row["market_value"]
                        )
                    ),
                    **shared_source_values,
                    "source_page": page_number,
                })

                continue

            if "USD" in line:
                raise MidasPdfParseError(
                    "Portfolio-summary row could not "
                    f"be parsed in {resolved_name}, "
                    f"page {page_number}: {line}"
                )

            continue

        if section == "trades":
            if not re.match(
                r"^\d{2}/\d{2}/\d{2}\s",
                line,
            ):
                continue

            gift_match = GIFT_TRADE_RE.match(line)

            if gift_match:
                row = gift_match.groupdict()

                commission = parse_turkish_number(
                    row["commission"]
                )

                if commission != 0:
                    raise MidasPdfParseError(
                        "Gift-share transaction has a "
                        f"commission in {resolved_name}, "
                        f"page {page_number}: {line}"
                    )

                trades.append({
                    "date": parse_pdf_date(
                        row["date"]
                    ),
                    "time": parse_pdf_time(
                        row["time"]
                    ),
                    "ticker": row["ticker"].upper(),
                    "side": "Buy",
                    "shares": float(
                        parse_turkish_number(
                            row["shares"]
                        )
                    ),
                    "price_usd": float(
                        parse_turkish_number(
                            row["price"]
                        )
                    ),
                    "commission_usd": 0.0,
                    "gross_amount_usd": float(
                        parse_turkish_number(
                            row["gross"]
                        )
                    ),
                    "is_gift": True,
                    **shared_source_values,
                    "source_page": page_number,
                })

                continue

            match = TRADE_RE.match(line)

            if not match:
                raise MidasPdfParseError(
                    "Trade row could not be parsed in "
                    f"{resolved_name}, page "
                    f"{page_number}: {line}"
                )

            row = match.groupdict()

            if row["status"] != "Gerçekleşti":
                ignored_order_count += 1
                continue

            if row["currency"] != "USD":
                raise MidasPdfParseError(
                    "Only USD trades are supported. "
                    f"Found {row['currency']} in "
                    f"{resolved_name}, page "
                    f"{page_number}."
                )

            trades.append({
                "date": parse_pdf_date(
                    row["date"]
                ),
                "time": parse_pdf_time(
                    row["time"]
                ),
                "ticker": row["ticker"].upper(),
                "side": (
                    "Buy"
                    if row["side"] == "Alış"
                    else "Sell"
                ),
                "shares": float(
                    parse_turkish_number(
                        row["shares"]
                    )
                ),
                "price_usd": float(
                    parse_turkish_number(
                        row["price"]
                    )
                ),
                "commission_usd": float(
                    parse_turkish_number(
                        row["commission"]
                    )
                ),
                "gross_amount_usd": float(
                    parse_turkish_number(
                        row["gross"]
                    )
                ),
                "is_gift": False,
                **shared_source_values,
                "source_page": page_number,
            })

            continue

        if section == "cash_movements":
            if not re.match(
                r"^\d{2}/\d{2}/\d{2}\s",
                line,
            ):
                continue

            match = CASH_MOVEMENT_RE.match(line)

            if not match:
                raise MidasPdfParseError(
                    "Cash-movement row could not be "
                    f"parsed in {resolved_name}, page "
                    f"{page_number}: {line}"
                )

            row = match.groupdict()

            if row["status"] != "Gerçekleşti":
                ignored_order_count += 1
                continue

            description = row["description"].strip()

            cash_movements.append({
                "date": parse_pdf_date(
                    row["date"]
                ),
                "time": parse_pdf_time(
                    row["time"]
                ),
                "type": classify_cash_movement(
                    description
                ),
                "amount_usd": float(
                    abs(
                        parse_turkish_number(
                            row["amount"]
                        )
                    )
                ),
                "description": description,
                "request_date": parse_pdf_date(
                    row["request_date"]
                ),
                "request_time": parse_pdf_time(
                    row["request_time"]
                ),
                **shared_source_values,
                "source_page": page_number,
            })

            continue

        if section == "dividends":
            if not re.match(
                r"^\d{2}/\d{2}/\d{2}\s",
                line,
            ):
                continue

            match = DIVIDEND_RE.match(line)

            if not match:
                raise MidasPdfParseError(
                    "Dividend row could not be parsed in "
                    f"{resolved_name}, page "
                    f"{page_number}: {line}"
                )

            row = match.groupdict()

            withholding_text = row["withholding"]

            withholding_tax = (
                parse_turkish_number(
                    withholding_text
                )
                if withholding_text is not None
                else Decimal("0")
            )

            dividends.append({
                "date": parse_pdf_date(
                    row["date"]
                ),
                "time": None,
                "ticker": row["ticker"].upper(),
                "company_name": (
                    row["company_name"].strip()
                ),
                "gross_dividend_usd": float(
                    parse_turkish_number(
                        row["gross"]
                    )
                ),
                "withholding_tax_usd": float(
                    withholding_tax
                ),
                "net_dividend_usd": float(
                    parse_turkish_number(
                        row["net"]
                    )
                ),
                **shared_source_values,
                "source_page": page_number,
            })

            continue

        if section == "share_transfers":
            if not re.match(
                r"^\d{2}/\d{2}/\d{2}\s",
                line,
            ):
                continue

            unsupported_activities.append({
                "section": "share_transfers",
                "raw_line": line,
                **shared_source_values,
                "source_page": page_number,
            })

    required_sections = {
        "trades",
        "cash_movements",
        "dividends",
    }

    missing_sections = (
        required_sections - sections_found
    )

    if missing_sections:
        raise MidasPdfParseError(
            f"Required sections are missing from "
            f"{resolved_name}: "
            + ", ".join(sorted(missing_sections))
        )

    if unsupported_activities:
        warnings.append(
            "Share-transfer rows were found. "
            "They require a separate validation step "
            "before import."
        )

    return ParsedMidasStatement(
        source_file=resolved_name,
        source_hash=source_hash,
        statement_start=statement_start,
        statement_end=statement_end,
        trades=pd.DataFrame(
            trades,
            columns=TRADE_COLUMNS,
        ),
        cash_movements=pd.DataFrame(
            cash_movements,
            columns=CASH_MOVEMENT_COLUMNS,
        ),
        dividends=pd.DataFrame(
            dividends,
            columns=DIVIDEND_COLUMNS,
        ),
        holdings=pd.DataFrame(
            holdings,
            columns=HOLDING_COLUMNS,
        ),
        unsupported_activities=pd.DataFrame(
            unsupported_activities,
            columns=UNSUPPORTED_ACTIVITY_COLUMNS,
        ),
        ignored_order_count=ignored_order_count,
        warnings=warnings,
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build the parser test command.
    """

    parser = ArgumentParser(
        description=(
            "Read one Midas account-statement PDF "
            "without modifying the portfolio database."
        )
    )

    parser.add_argument(
        "pdf_path",
        type=Path,
        help="Path to one Midas PDF statement.",
    )

    return parser


def main() -> None:
    """
    Parse one PDF and print a safe summary.
    """

    arguments = build_argument_parser().parse_args()

    result = parse_midas_statement(
        arguments.pdf_path
    )

    print("Midas statement parsed successfully.")
    print()

    for key, value in result.summary().items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
