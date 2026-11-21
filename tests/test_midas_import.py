from datetime import date, time

import pandas as pd

import midas_import_service
import midas_pdf_parser
from midas_import_service import (
    MidasStatementInput,
    load_midas_statements,
)
from midas_pdf_parser import (
    CASH_MOVEMENT_COLUMNS,
    DIVIDEND_COLUMNS,
    HOLDING_COLUMNS,
    TRADE_COLUMNS,
    UNSUPPORTED_ACTIVITY_COLUMNS,
    ParsedMidasStatement,
    parse_midas_statement,
)


class FakePdfPage:
    """
    Represent one text-based PDF page.
    """

    def __init__(self, text: str):
        self.text = text

    def extract_text(self) -> str:
        return self.text


class FakePdf:
    """
    Behave like the pdfplumber PDF context manager.
    """

    def __init__(self, pages: list[str]):
        self.pages = [
            FakePdfPage(text)
            for text in pages
        ]

    def __enter__(self):
        return self

    def __exit__(
        self,
        exception_type,
        exception,
        traceback,
    ):
        return False


def install_fake_pdf(
    monkeypatch,
    page_texts: list[str],
) -> None:
    """
    Replace pdfplumber with deterministic test pages.
    """

    monkeypatch.setattr(
        midas_pdf_parser.pdfplumber,
        "open",
        lambda *args, **kwargs: FakePdf(
            page_texts
        ),
    )


def empty_frame(
    columns: list[str],
) -> pd.DataFrame:
    """
    Create an empty parser result table.
    """

    return pd.DataFrame(columns=columns)


def make_trade(
    source_file: str,
    source_hash: str,
    statement_start: date,
    statement_end: date,
    trade_date: date,
    trade_time: time,
    ticker: str = "AAPL",
) -> dict:
    """
    Create one representative parsed trade.
    """

    return {
        "date": trade_date,
        "time": trade_time,
        "ticker": ticker,
        "side": "Buy",
        "shares": 1.0,
        "price_usd": 100.0,
        "commission_usd": 1.5,
        "gross_amount_usd": 100.0,
        "is_gift": False,
        "source_file": source_file,
        "source_hash": source_hash,
        "statement_start": statement_start,
        "statement_end": statement_end,
        "source_page": 1,
    }


def make_statement(
    source_file: str,
    source_hash: str,
    statement_start: date,
    statement_end: date,
    trades: list[dict] | None = None,
) -> ParsedMidasStatement:
    """
    Create a parsed statement without a real PDF.
    """

    return ParsedMidasStatement(
        source_file=source_file,
        source_hash=source_hash,
        statement_start=statement_start,
        statement_end=statement_end,
        trades=pd.DataFrame(
            trades or [],
            columns=TRADE_COLUMNS,
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
        ignored_order_count=0,
        warnings=[],
    )


def test_parser_reads_supported_midas_records(
    monkeypatch,
):
    """
    The parser must extract raw financial records without
    applying splits or ticker changes.
    """

    statement_text = """
01/01/24 - 31/01/24 HESAP EKSTRESİ
PORTFÖY ÖZETİ (31/01/24)
Sermaye Piyasası Aracı Adet Hisse Baş Ortalama Maliyet Kar / Zarar Toplam Değeri
AAPL - Apple 2 100,00 USD 10,00 USD 220,00 USD
YATIRIM İŞLEMLERİ (01/01/24 - 31/01/24)
Tarih İşlem Türü Sembol İşlem Tipi İşlem Durumu Para Birimi Emir Adedi Emir Tutarı Gerçekleşen Adet Ortalama İşlem Fiyatı İşlem Ücreti İşlem Tutarı
02/01/24 10:00:00 Piyasa Emri AAPL Alış Gerçekleşti USD 2 201,50 2 100,00 1,50 200,00
03/01/24 11:00:00 Hediye Hisse MSFT Hediye Hisse Gerçekleşti USD - - 0,5 200,00 0,00 0,00
04/01/24 12:00:00 Piyasa Emri AAPL Satış İptal Edildi USD 1 - 0 - - 0,00
HESAP İŞLEMLERİ (01/01/24 - 31/01/24)
Talep Tarihi İşlem Tarihi İşlem Tipi İşlem Açıklaması İşlem Durumu Tutar
02/01/24 09:00:00 02/01/24 09:01:00 Para Yatırma Gerçekleşti 500,00 USD
31/01/24 20:00:00 31/01/24 20:00:00 Diğer Gelir Nema Geliri Gerçekleşti 0,50 USD
TEMETTÜ İŞLEMLERİ (01/01/24 - 31/01/24)
Ödeme Tarihi Sermaye Piyasası Aracı Brüt Temettü Tutarı Stopaj Net Temettü Tutarı
15/01/24 AAPL - Apple 2,00 USD 0,40 USD 1,60 USD
HİSSE TRANSFERLERİ (01/01/24 - 31/01/24)
Kayıt bulunmamaktadır.
"""

    install_fake_pdf(
        monkeypatch,
        [statement_text],
    )

    result = parse_midas_statement(
        b"%PDF-fake-content",
        source_name="Ocak 2024.pdf",
    )

    assert result.statement_start == date(
        2024,
        1,
        1,
    )

    assert result.statement_end == date(
        2024,
        1,
        31,
    )

    assert len(result.trades) == 2
    assert len(result.cash_movements) == 2
    assert len(result.dividends) == 1
    assert len(result.holdings) == 1
    assert result.ignored_order_count == 1

    normal_trade = result.trades.iloc[0]
    gift_trade = result.trades.iloc[1]

    assert normal_trade["ticker"] == "AAPL"
    assert normal_trade["shares"] == 2.0
    assert normal_trade["price_usd"] == 100.0
    assert normal_trade["commission_usd"] == 1.5
    assert bool(normal_trade["is_gift"]) is False

    assert gift_trade["ticker"] == "MSFT"
    assert gift_trade["shares"] == 0.5
    assert gift_trade["commission_usd"] == 0.0
    assert bool(gift_trade["is_gift"]) is True

    assert (
        result.cash_movements.iloc[0]["type"]
        == "DEPOSIT"
    )

    assert (
        result.cash_movements.iloc[1]["type"]
        == "INTEREST"
    )

    dividend = result.dividends.iloc[0]

    assert dividend["gross_dividend_usd"] == 2.0
    assert dividend["withholding_tax_usd"] == 0.4
    assert dividend["net_dividend_usd"] == 1.6


def test_parser_accepts_month_without_trades(
    monkeypatch,
):
    """
    A valid statement may contain no investment activity.
    """

    statement_text = """
01/12/24 - 31/12/24 HESAP EKSTRESİ
PORTFÖY ÖZETİ (31/12/24)
Sermaye Piyasası Aracı Adet Hisse Baş Ortalama Maliyet Kar / Zarar Toplam Değeri
AAPL - Apple 2 100,00 USD 20,00 USD 240,00 USD
YATIRIM İŞLEMLERİ (01/12/24 - 31/12/24)
Kayıt bulunmamaktadır.
HESAP İŞLEMLERİ (01/12/24 - 31/12/24)
Kayıt bulunmamaktadır.
TEMETTÜ İŞLEMLERİ (01/12/24 - 31/12/24)
Kayıt bulunmamaktadır.
HİSSE TRANSFERLERİ (01/12/24 - 31/12/24)
Kayıt bulunmamaktadır.
"""

    install_fake_pdf(
        monkeypatch,
        [statement_text],
    )

    result = parse_midas_statement(
        b"%PDF-no-trades",
        source_name="Aralık 2024.pdf",
    )

    assert result.trades.empty
    assert result.cash_movements.empty
    assert result.dividends.empty
    assert len(result.holdings) == 1


def test_identical_pdf_is_skipped(
    monkeypatch,
):
    """
    Uploading the same PDF twice must not duplicate data.
    """

    statement = make_statement(
        source_file="Ocak 2024.pdf",
        source_hash="same-pdf-hash",
        statement_start=date(2024, 1, 1),
        statement_end=date(2024, 1, 31),
    )

    monkeypatch.setattr(
        midas_import_service,
        "parse_midas_statement",
        lambda source, source_name=None: (
            statement
        ),
    )

    result = load_midas_statements(
        [
            MidasStatementInput(
                source=b"%PDF-first",
                source_name="Ocak 2024.pdf",
            ),
            MidasStatementInput(
                source=b"%PDF-second",
                source_name="Ocak kopya.pdf",
            ),
        ]
    )

    assert len(result.statement_summaries) == 1
    assert result.duplicate_pdf_names == [
        "Ocak 2024.pdf"
    ]


def test_cross_statement_duplicate_is_removed_but_real_repetition_is_kept(
    monkeypatch,
):
    """
    Two identical trades inside one statement are retained,
    while the repeated copy in another statement is removed.
    """

    april_start = date(2023, 4, 1)
    april_end = date(2023, 4, 30)
    may_start = date(2023, 5, 1)
    may_end = date(2023, 5, 31)

    trade_date = date(2023, 4, 29)
    trade_time = time(1, 31, 25)

    april_trade = make_trade(
        source_file="Nisan 2023.pdf",
        source_hash="april-hash",
        statement_start=april_start,
        statement_end=april_end,
        trade_date=trade_date,
        trade_time=trade_time,
        ticker="AMZN",
    )

    april_statement = make_statement(
        source_file="Nisan 2023.pdf",
        source_hash="april-hash",
        statement_start=april_start,
        statement_end=april_end,
        trades=[
            april_trade.copy(),
            april_trade.copy(),
        ],
    )

    may_trade = make_trade(
        source_file="Mayıs 2023.pdf",
        source_hash="may-hash",
        statement_start=may_start,
        statement_end=may_end,
        trade_date=trade_date,
        trade_time=trade_time,
        ticker="AMZN",
    )

    may_statement = make_statement(
        source_file="Mayıs 2023.pdf",
        source_hash="may-hash",
        statement_start=may_start,
        statement_end=may_end,
        trades=[may_trade],
    )

    statements = {
        "Nisan 2023.pdf": april_statement,
        "Mayıs 2023.pdf": may_statement,
    }

    monkeypatch.setattr(
        midas_import_service,
        "parse_midas_statement",
        lambda source, source_name=None: (
            statements[source_name]
        ),
    )

    result = load_midas_statements(
        [
            MidasStatementInput(
                source=b"%PDF-april",
                source_name="Nisan 2023.pdf",
            ),
            MidasStatementInput(
                source=b"%PDF-may",
                source_name="Mayıs 2023.pdf",
            ),
        ]
    )

    assert len(result.trades) == 2

    assert (
        result.duplicates_removed["trades"]
        == 1
    )

    assert set(
        result.trades["source_file"]
    ) == {"Nisan 2023.pdf"}


def test_missing_statement_month_is_warning_not_error(
    monkeypatch,
):
    """
    Missing no-activity months must create a warning without
    blocking statement preparation.
    """

    january = make_statement(
        source_file="Ocak 2024.pdf",
        source_hash="january-hash",
        statement_start=date(2024, 1, 1),
        statement_end=date(2024, 1, 31),
    )

    march = make_statement(
        source_file="Mart 2024.pdf",
        source_hash="march-hash",
        statement_start=date(2024, 3, 1),
        statement_end=date(2024, 3, 31),
    )

    statements = {
        "Ocak 2024.pdf": january,
        "Mart 2024.pdf": march,
    }

    monkeypatch.setattr(
        midas_import_service,
        "parse_midas_statement",
        lambda source, source_name=None: (
            statements[source_name]
        ),
    )

    result = load_midas_statements(
        [
            MidasStatementInput(
                source=b"%PDF-january",
                source_name="Ocak 2024.pdf",
            ),
            MidasStatementInput(
                source=b"%PDF-march",
                source_name="Mart 2024.pdf",
            ),
        ]
    )

    assert len(result.statement_summaries) == 2

    assert any(
        "gap was detected" in warning
        for warning in result.warnings
    )
