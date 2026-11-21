from __future__ import annotations

from argparse import ArgumentParser
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from time import sleep
from typing import Any
from urllib.parse import urlencode
import os
import re

import requests


SEC_SEARCH_URL = (
    "https://efts.sec.gov/LATEST/search-index"
)

DEFAULT_SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT"
)


def resolve_sec_user_agent(
    sec_user_agent: str | None = None,
) -> str:
    """
    Resolve the application operator identity required by
    SEC fair-access rules.

    This identifies the application operator, not the
    portfolio end user.
    """

    resolved_value = (
        sec_user_agent
        or DEFAULT_SEC_USER_AGENT
        or os.getenv("SEC_USER_AGENT")
    )

    if not resolved_value:
        raise RuntimeError(
            "SEC access is not configured. Set the "
            "SEC_USER_AGENT environment variable to an "
            "application name and operator contact email."
        )

    resolved_value = resolved_value.strip()

    if "@" not in resolved_value:
        raise RuntimeError(
            "SEC_USER_AGENT must include the application "
            "operator's contact email address."
        )

    return resolved_value

SEC_FORMS = (
    "8-K,SC 13D,SC 13D/A,25-NSE,"
    "DEFA14A,PREM14A,SC TO-T"
)

MONTH_PATTERN = (
    r"(?:January|February|March|April|May|June|"
    r"July|August|September|October|November|"
    r"December)"
)

DATE_PATTERN = (
    rf"{MONTH_PATTERN}\s+\d{{1,2}},\s+\d{{4}}"
)

PRICE_PATTERN = r"\$([0-9]+(?:\.[0-9]{1,4})?)"


@dataclass(frozen=True)
class SecSearchHit:
    """
    One SEC full-text-search result.
    """

    company_name: str
    cik: str
    form: str
    filing_date: date
    accession_number: str
    filename: str
    document_url: str


@dataclass(frozen=True)
class SecDocumentEvidence:
    """
    Cash-acquisition evidence extracted from one filing.
    """

    hit: SecSearchHit
    cash_price_per_share: float
    event_date: date | None
    evidence_score: float
    context: str


@dataclass
class SecCashAcquisitionResult:
    """
    Aggregated official SEC evidence.
    """

    status: str
    ticker: str
    company_query: str
    cash_price_per_share: float | None
    event_date: date | None
    supporting_document_count: int
    best_document_url: str | None
    best_document_form: str | None
    best_document_filing_date: date | None
    evidence: list[SecDocumentEvidence]
    warnings: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ticker": self.ticker,
            "company_query": self.company_query,
            "cash_price_per_share": (
                self.cash_price_per_share
            ),
            "event_date": (
                self.event_date.isoformat()
                if self.event_date
                else None
            ),
            "supporting_document_count": (
                self.supporting_document_count
            ),
            "best_document_form": (
                self.best_document_form
            ),
            "best_document_filing_date": (
                self.best_document_filing_date
                .isoformat()
                if self.best_document_filing_date
                else None
            ),
            "best_document_url": (
                self.best_document_url
            ),
            "warnings": self.warnings.copy(),
        }


class FilingTextExtractor(HTMLParser):
    """
    Convert SEC HTML filings to searchable plain text.
    """

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attributes,
    ) -> None:
        if tag.lower() in {
            "script",
            "style",
        }:
            self.ignored_depth += 1

    def handle_endtag(
        self,
        tag: str,
    ) -> None:
        if (
            tag.lower() in {"script", "style"}
            and self.ignored_depth > 0
        ):
            self.ignored_depth -= 1

    def handle_data(
        self,
        data: str,
    ) -> None:
        if self.ignored_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(
            r"\s+",
            " ",
            " ".join(self.parts),
        ).strip()


def build_sec_session(
    sec_user_agent: str | None = None,
) -> requests.Session:
    """
    Create a declared, read-only SEC HTTP session.
    """

    declared_user_agent = (
        resolve_sec_user_agent(
            sec_user_agent
        )
    )

    session = requests.Session()

    session.headers.update({
        "User-Agent": declared_user_agent,
        "Accept": (
            "application/json,"
            "text/html,"
            "application/xhtml+xml"
        ),
        "Accept-Encoding": "gzip, deflate",
        "Referer": (
            "https://www.sec.gov/edgar/search/"
        ),
    })

    return session

def parse_iso_date(
    value: str,
) -> date:
    """
    Parse SEC YYYY-MM-DD date values.
    """

    return datetime.strptime(
        value,
        "%Y-%m-%d",
    ).date()


def parse_written_date(
    value: str,
) -> date | None:
    """
    Parse dates such as October 27, 2022.
    """

    try:
        return datetime.strptime(
            value,
            "%B %d, %Y",
        ).date()

    except ValueError:
        return None


def normalize_company_query(
    company_name: str,
) -> str:
    """
    Reduce a statement display name to useful SEC terms.
    """

    cleaned = re.sub(
        r"\.{2,}",
        " ",
        company_name,
    )

    cleaned = re.sub(
        r"[^A-Za-z0-9 ]+",
        " ",
        cleaned,
    )

    stop_words = {
        "inc",
        "incorporated",
        "corporation",
        "corp",
        "company",
        "co",
        "class",
        "common",
        "stock",
        "holdings",
        "holding",
        "group",
        "limited",
        "ltd",
        "plc",
        "the",
    }

    tokens = [
        token
        for token in cleaned.split()
        if (
            token.casefold()
            not in stop_words
        )
    ]

    if not tokens:
        return company_name.strip()

    return " ".join(tokens[:3])


def build_search_queries(
    ticker: str,
    company_name: str,
) -> list[str]:
    """
    Build progressively broader SEC searches.
    """

    company_query = normalize_company_query(
        company_name
    )

    queries = [
        (
            f'"{company_query}" '
            f'"per share" cash'
        ),
        (
            f'"{company_query}" '
            "merger cash"
        ),
        (
            f'"{company_query}" '
            "acquisition cash"
        ),
        (
            f'"{ticker}" '
            f'"per share" cash'
        ),
    ]

    return list(dict.fromkeys(queries))


def build_document_url(
    hit_id: str,
    source: dict[str, Any],
) -> SecSearchHit | None:
    """
    Convert an SEC search result to an archive URL.
    """

    if ":" not in hit_id:
        return None

    accession_number, filename = (
        hit_id.split(":", 1)
    )

    ciks = source.get("ciks") or []

    if not ciks:
        return None

    cik = str(ciks[0]).lstrip("0")

    if not cik:
        return None

    filing_date_text = source.get(
        "file_date"
    )

    if not filing_date_text:
        return None

    filing_date = parse_iso_date(
        filing_date_text
    )

    accession_folder = (
        accession_number.replace("-", "")
    )

    document_url = (
        "https://www.sec.gov/Archives/"
        f"edgar/data/{cik}/"
        f"{accession_folder}/{filename}"
    )

    display_names = (
        source.get("display_names") or []
    )

    company_name = (
        str(display_names[0])
        if display_names
        else ""
    )

    return SecSearchHit(
        company_name=company_name,
        cik=cik,
        form=str(
            source.get("form") or ""
        ),
        filing_date=filing_date,
        accession_number=accession_number,
        filename=filename,
        document_url=document_url,
    )


def search_sec_filings(
    session: requests.Session,
    ticker: str,
    company_name: str,
    start_date: date,
    end_date: date,
) -> list[SecSearchHit]:
    """
    Search SEC full-text filings around a disappearance.
    """

    hits_by_url: dict[
        str,
        SecSearchHit,
    ] = {}

    for query in build_search_queries(
        ticker=ticker,
        company_name=company_name,
    ):
        parameters = {
            "q": query,
            "dateRange": "custom",
            "startdt": start_date.isoformat(),
            "enddt": end_date.isoformat(),
            "forms": SEC_FORMS,
            "from": 0,
        }

        response = session.get(
            SEC_SEARCH_URL,
            params=parameters,
            timeout=30,
        )

        response.raise_for_status()

        payload = response.json()

        raw_hits = (
            payload.get("hits", {})
            .get("hits", [])
        )

        for raw_hit in raw_hits:
            hit = build_document_url(
                hit_id=str(
                    raw_hit.get("_id") or ""
                ),
                source=(
                    raw_hit.get("_source")
                    or {}
                ),
            )

            if hit is None:
                continue

            hits_by_url[
                hit.document_url
            ] = hit

        # Respect SEC fair-access limits.
        sleep(0.15)

    return sorted(
        hits_by_url.values(),
        key=lambda hit: (
            hit.filing_date,
            hit.form,
            hit.document_url,
        ),
    )


def html_to_text(
    content: str,
) -> str:
    """
    Convert a filing document to normalized text.
    """

    parser = FilingTextExtractor()
    parser.feed(content)

    extracted = parser.text()

    if extracted:
        return extracted

    return re.sub(
        r"\s+",
        " ",
        re.sub(
            r"<[^>]+>",
            " ",
            content,
        ),
    ).strip()


def price_context_score(
    context: str,
) -> float:
    """
    Score whether a dollar value describes a cash merger.
    """

    normalized = context.casefold()
    score = 0.0

    if "cash" in normalized:
        score += 3.0

    if (
        "per share" in normalized
        or "each share" in normalized
        or "each issued" in normalized
    ):
        score += 4.0

    if (
        "merger" in normalized
        or "acquisition" in normalized
    ):
        score += 3.0

    if (
        "converted" in normalized
        or "canceled" in normalized
        or "cancelled" in normalized
    ):
        score += 2.0

    if (
        "common stock" in normalized
        or "stockholder" in normalized
    ):
        score += 1.0

    return score


def extract_cash_prices(
    filing_text: str,
) -> list[
    tuple[float, float, str]
]:
    """
    Extract cash-per-share candidates with their context.
    """

    patterns = [
        re.compile(
            rf"(?:each|every)\s+"
            rf".{{0,180}}?\bshare\b"
            rf".{{0,260}}?"
            rf"{PRICE_PATTERN}\s+"
            rf"(?:in\s+)?cash",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{PRICE_PATTERN}\s+"
            rf"(?:in\s+)?cash"
            rf".{{0,160}}?"
            rf"(?:per|for each)\s+share",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{PRICE_PATTERN}\s+"
            rf"per\s+share"
            rf".{{0,100}}?cash",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?:per|for each)\s+share"
            rf".{{0,120}}?"
            rf"{PRICE_PATTERN}",
            re.IGNORECASE,
        ),
    ]

    candidates: list[
        tuple[float, float, str]
    ] = []

    for pattern in patterns:
        for match in pattern.finditer(
            filing_text
        ):
            price = float(
                match.group(1)
            )

            if not 0.01 <= price <= 100000:
                continue

            context_start = max(
                0,
                match.start() - 250,
            )

            context_end = min(
                len(filing_text),
                match.end() + 250,
            )

            context = filing_text[
                context_start:context_end
            ]

            score = price_context_score(
                context
            )

            if score < 7:
                continue

            candidates.append(
                (
                    price,
                    score,
                    context,
                )
            )

    return candidates


def extract_event_dates(
    filing_text: str,
    start_date: date,
    end_date: date,
) -> list[date]:
    """
    Extract merger completion/effective dates.
    """

    patterns = [
        re.compile(
            rf"\bOn\s+({DATE_PATTERN})"
            rf".{{0,500}}?"
            rf"(?:merger|acquisition)"
            rf".{{0,250}}?"
            rf"(?:consummated|completed|"
            rf"became effective|was effective)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?:merger|acquisition)"
            rf".{{0,300}}?"
            rf"(?:consummated|completed|"
            rf"became effective|was effective)"
            rf".{{0,180}}?"
            rf"({DATE_PATTERN})",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?:effective time|closing date)"
            rf".{{0,180}}?"
            rf"({DATE_PATTERN})",
            re.IGNORECASE,
        ),
    ]

    dates: list[date] = []

    for pattern in patterns:
        for match in pattern.finditer(
            filing_text
        ):
            parsed = parse_written_date(
                match.group(1)
            )

            if parsed is None:
                continue

            if (
                start_date
                <= parsed
                <= end_date
            ):
                dates.append(parsed)

    return dates


def inspect_sec_document(
    session: requests.Session,
    hit: SecSearchHit,
    start_date: date,
    end_date: date,
) -> list[SecDocumentEvidence]:
    """
    Download and inspect one official filing document.
    """

    response = session.get(
        hit.document_url,
        timeout=30,
    )

    response.raise_for_status()

    filing_text = html_to_text(
        response.text
    )

    price_candidates = extract_cash_prices(
        filing_text
    )

    if not price_candidates:
        return []

    event_dates = extract_event_dates(
        filing_text=filing_text,
        start_date=start_date,
        end_date=end_date,
    )

    event_date = (
        Counter(event_dates)
        .most_common(1)[0][0]
        if event_dates
        else None
    )

    evidence: list[
        SecDocumentEvidence
    ] = []

    for price, score, context in (
        price_candidates
    ):
        evidence.append(
            SecDocumentEvidence(
                hit=hit,
                cash_price_per_share=price,
                event_date=event_date,
                evidence_score=score,
                context=context,
            )
        )

    sleep(0.15)

    return evidence

def select_cash_price_evidence(
    all_evidence: list[SecDocumentEvidence],
    expected_price_per_share: (
        float | None
    ) = None,
) -> tuple[
    float,
    list[SecDocumentEvidence],
]:
    """
    Select the strongest cash-per-share evidence.

    When Midas cash evidence provides an implied price,
    nearby official SEC prices are preferred.
    """

    if not all_evidence:
        raise ValueError(
            "At least one SEC evidence item is required."
        )

    grouped_evidence: dict[
        float,
        list[SecDocumentEvidence],
    ] = {}

    for evidence in all_evidence:
        rounded_price = round(
            evidence.cash_price_per_share,
            6,
        )

        grouped_evidence.setdefault(
            rounded_price,
            [],
        ).append(evidence)

    def evidence_strength(
        price: float,
    ) -> tuple[int, int, float]:
        items = grouped_evidence[price]

        document_count = len({
            item.hit.document_url
            for item in items
        })

        maximum_score = max(
            item.evidence_score
            for item in items
        )

        return (
            len(items),
            document_count,
            maximum_score,
        )

    selected_price: float | None = None

    if expected_price_per_share is not None:
        expected_price = float(
            expected_price_per_share
        )

        if expected_price <= 0:
            raise ValueError(
                "Expected cash price must be positive."
            )

        nearby_prices = [
            price
            for price in grouped_evidence
            if (
                abs(price - expected_price)
                / expected_price
                <= 0.02
            )
        ]

        if nearby_prices:
            selected_price = min(
                nearby_prices,
                key=lambda price: (
                    abs(
                        price - expected_price
                    )
                    / expected_price,
                    -evidence_strength(price)[1],
                    -evidence_strength(price)[0],
                    -evidence_strength(price)[2],
                ),
            )

    if selected_price is None:
        selected_price = max(
            grouped_evidence,
            key=evidence_strength,
        )

    return (
        selected_price,
        grouped_evidence[selected_price],
    )

def find_sec_cash_acquisition(
    ticker: str,
    company_name: str,
    start_date: date,
    end_date: date,
    maximum_documents: int = 12,
    sec_user_agent: str | None = None,
    expected_price_per_share: (
        float | None
    ) = None,
) -> SecCashAcquisitionResult:
    """
    Find official cash-acquisition evidence in SEC filings.
    """

    normalized_ticker = (
        ticker.strip().upper()
    )

    company_query = normalize_company_query(
        company_name
    )

    session = build_sec_session(
        sec_user_agent=sec_user_agent
    )
    warnings: list[str] = []

    try:
        hits = search_sec_filings(
            session=session,
            ticker=normalized_ticker,
            company_name=company_name,
            start_date=start_date,
            end_date=end_date,
        )

    except Exception as error:
        return SecCashAcquisitionResult(
            status="LOOKUP_FAILED",
            ticker=normalized_ticker,
            company_query=company_query,
            cash_price_per_share=None,
            event_date=None,
            supporting_document_count=0,
            best_document_url=None,
            best_document_form=None,
            best_document_filing_date=None,
            evidence=[],
            warnings=[str(error)],
        )

    all_evidence: list[
        SecDocumentEvidence
    ] = []

    for hit in hits[:maximum_documents]:
        try:
            all_evidence.extend(
                inspect_sec_document(
                    session=session,
                    hit=hit,
                    start_date=start_date,
                    end_date=end_date,
                )
            )

        except Exception as error:
            warnings.append(
                f"{hit.document_url}: {error}"
            )

    if not all_evidence:
        return SecCashAcquisitionResult(
            status="NOT_FOUND",
            ticker=normalized_ticker,
            company_query=company_query,
            cash_price_per_share=None,
            event_date=None,
            supporting_document_count=0,
            best_document_url=None,
            best_document_form=None,
            best_document_filing_date=None,
            evidence=[],
            warnings=warnings,
        )

    (
        selected_price,
        selected_evidence,
    ) = select_cash_price_evidence(
        all_evidence=all_evidence,
        expected_price_per_share=(
            expected_price_per_share
        ),
    )

    selected_evidence.sort(
        key=lambda item: (
            -item.evidence_score,
            item.hit.filing_date,
        )
    )

    best = selected_evidence[0]

    event_dates = [
        item.event_date
        for item in selected_evidence
        if item.event_date is not None
    ]

    event_date = (
        Counter(event_dates)
        .most_common(1)[0][0]
        if event_dates
        else None
    )

    unique_documents = {
        item.hit.document_url
        for item in selected_evidence
    }

    return SecCashAcquisitionResult(
        status="FOUND",
        ticker=normalized_ticker,
        company_query=company_query,
        cash_price_per_share=(
            float(selected_price)
        ),
        event_date=event_date,
        supporting_document_count=len(
            unique_documents
        ),
        best_document_url=(
            best.hit.document_url
        ),
        best_document_form=best.hit.form,
        best_document_filing_date=(
            best.hit.filing_date
        ),
        evidence=selected_evidence,
        warnings=warnings,
    )


def build_argument_parser() -> ArgumentParser:
    """
    Build terminal-test arguments.
    """

    parser = ArgumentParser(
        description=(
            "Search official SEC filings for a "
            "cash acquisition."
        )
    )

    parser.add_argument(
        "--ticker",
        required=True,
    )

    parser.add_argument(
        "--company",
        required=True,
    )

    parser.add_argument(
        "--start-date",
        required=True,
        type=parse_iso_date,
    )

    parser.add_argument(
        "--end-date",
        required=True,
        type=parse_iso_date,
    )

    return parser


def main() -> None:
    """
    Test official SEC lookup.
    """

    arguments = build_argument_parser().parse_args()

    result = find_sec_cash_acquisition(
        ticker=arguments.ticker,
        company_name=arguments.company,
        start_date=arguments.start_date,
        end_date=arguments.end_date,
    )

    print()
    print("SEC corporate-action lookup completed.")
    print()

    for key, value in result.summary().items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
